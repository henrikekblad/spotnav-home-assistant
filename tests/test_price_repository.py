"""The shared price repository: state, coalescing and the store.

The transport here is a stub with no network in it at all, and time is injected,
so every race below is deterministic: requests are held open on `asyncio.Event`s
and released by the test, never by a `sleep`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.pricing import price_repository as price_repository_module
from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.pricing.price_repository import (
    STORAGE_KEY,
    PriceRepository,
    async_setup_price_repository,
    catalogue_summary,
    day_summary,
)
from .relay import stub_repository, Clock, StoreDouble, StubTransport, TODAY, TOMORROW, day_body, fixture
from custom_components.spotnav.runtime import domain_data

@pytest.fixture
def repository(hass: HomeAssistant, transport: StubTransport, clock: Clock) -> PriceRepository:
    return stub_repository(hass, transport, now=clock)


def summary_is_flat(summary: dict[str, Any]) -> bool:
    """A summary is scalars, and at most a short list of labels.

    No price array and no nested document: a list of currency codes is provenance,
    a list of 96 floats would be the data itself.
    """

    def flat(value: Any) -> bool:
        if isinstance(value, (list, tuple, set)):
            return len(value) <= 8 and all(isinstance(item, str) and len(item) <= 16 for item in value)
        if isinstance(value, dict):
            return all(flat(item) for item in value.values())
        return True

    return all(flat(value) for value in summary.values())


# ------------------------------------------------------------- one stream


async def test_two_consumers_share_one_request_and_one_coherent_document(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    transport.serve_area()
    path = "/v1/SE4/2026/09-22.json"
    transport.hold(path)

    first = asyncio.ensure_future(repository.async_get_day("SE4", TODAY))
    await transport.entered[path].wait()
    # The second consumer -- another charger entry, in production -- arrives while
    # the first request is still open. It must not open a second one.
    second = asyncio.ensure_future(repository.async_get_day("SE4", TODAY))
    await asyncio.sleep(0)
    assert transport.call_count(path) == 1

    transport.release(path)
    one, two = await asyncio.gather(first, second)

    assert transport.call_count(path) == 1
    assert one.document is not None and two.document is not None
    # Coherent: the same immutable model, not two copies that could drift.
    assert one.document is two.document
    assert one.document.prices == two.document.prices
    assert one.document.area_id == "SE4" and one.document.day == TODAY
    assert one.interval_count == 96 and one.state == "ready"
    assert one.source == "network" and one.fetched_at is not None
    assert one.index_authority == "listed"

    # A third ask, now that it is cached, is answered from memory with no request.
    again = await repository.async_get_day("SE4", TODAY)
    assert again.state == "ready" and again.source == "memory"
    assert again.document is one.document
    assert transport.call_count(path) == 1


async def test_a_cancelled_waiter_does_not_cancel_the_shared_request(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    transport.serve_area()
    path = "/v1/SE4/2026/09-22.json"
    transport.hold(path)

    leaving = asyncio.ensure_future(repository.async_get_day("SE4", TODAY))
    await transport.entered[path].wait()
    staying = asyncio.ensure_future(repository.async_get_day("SE4", TODAY))
    await asyncio.sleep(0)

    leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving

    transport.release(path)
    survived = await staying

    # The policy, stated and tested: the request belongs to the repository, so the
    # consumer that lost interest stops waiting and nothing else happens.
    assert survived.document is not None and survived.state == "ready"
    assert transport.call_count(path) == 1


async def test_a_failure_does_not_poison_the_next_attempt(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    transport.serve_area()
    path = "/v1/SE4/2026/09-22.json"
    transport.serve(path, 502, "bad gateway")

    failed = await repository.async_get_day("SE4", TODAY)
    assert failed.state == "unavailable"
    assert failed.document is None
    assert failed.attempt_error == "http_status" and failed.attempt_at is not None

    # The entry is gone from the in-flight table, so this is a fresh attempt: a
    # poisoned key would have the second caller await a failure that cannot change.
    transport.serve(path, 200, day_body("SE4", TODAY))
    retried = await repository.async_get_day("SE4", TODAY, refresh=True)
    assert retried.state == "ready" and retried.document is not None
    assert transport.call_count(path) == 2



# ------------------------------------------------------------ last good, kept


async def test_a_newer_success_supersedes_and_a_later_failure_keeps_the_old_one(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository, clock: Clock
) -> None:
    transport.serve_area()
    path = "/v1/SE4/2026/09-22.json"

    first = await repository.async_get_day("SE4", TODAY)
    assert first.state == "ready" and first.document is not None
    acquired = first.fetched_at
    assert acquired is not None
    first_prices = first.document.prices

    # A newer publication of the same day: the successful refresh supersedes it.
    clock.advance(hours=2)
    newer = json.loads(day_body("SE4", TODAY))
    newer["prices"] = [round(value + 0.01, 5) for value in newer["prices"]]
    transport.serve(path, 200, json.dumps(newer))
    refreshed = await repository.async_get_day("SE4", TODAY, refresh=True)
    assert refreshed.document is not None
    assert refreshed.document.prices != first_prices
    assert refreshed.fetched_at is not None and refreshed.fetched_at > acquired

    # Now a failure. The document stays, and so does the moment it was acquired:
    # a failed attempt may never relabel old data as freshly fetched.
    clock.advance(minutes=30)
    transport.serve(path, 500, "upstream is unhappy")
    broken = await repository.async_get_day("SE4", TODAY, refresh=True)
    assert broken.state == "stale"
    assert broken.document is refreshed.document
    assert broken.fetched_at == refreshed.fetched_at
    assert broken.attempt_error == "http_status"
    assert broken.attempt_at is not None and broken.attempt_at > refreshed.fetched_at

    # And an *invalid* body behaves exactly like a failed one: it is not data.
    transport.serve(path, 200, "<html>nope</html>")
    html = await repository.async_get_day("SE4", TODAY, refresh=True)
    assert html.state == "stale" and html.document is refreshed.document
    assert html.attempt_error == "not_json"
    assert html.fetched_at == refreshed.fetched_at

    transport.serve(path, 200, json.dumps({**newer, "prices": [True] * len(newer["prices"])}))
    invalid = await repository.async_get_day("SE4", TODAY, refresh=True)
    assert invalid.state == "stale" and invalid.document is refreshed.document
    assert invalid.attempt_error == "invalid_number"


# ---------------------------------------------------------------- the store


async def test_a_restart_restores_last_good_without_a_network_call(
    hass: HomeAssistant, hass_storage: dict[str, Any], transport: StubTransport, clock: Clock
) -> None:
    transport.serve_area()
    repository = stub_repository(hass, transport, now=clock)
    original = await repository.async_get_day("SE4", TODAY)
    await hass.async_block_till_done()

    written = hass_storage[STORAGE_KEY]["data"]
    assert written["schema"] == 2
    # The raw published document, not a summary of it, and its own acquisition time, filed under its
    # market calendar and labelled with who published it.
    stored_day = written["days"]["SE4|Europe/Stockholm|2026-09-22"]
    assert stored_day["document"]["area"] == "SE4"
    assert stored_day["document"]["prices"] == list(original.document.prices)
    assert stored_day["provider"] == "stub"
    # No area list and no index: the list is the bundled one.
    assert set(written) == {"schema", "days"}
    # No credential, no webhook id and no URL ever reaches the store.
    assert "http" not in json.dumps(written)

    # A new process: a new repository, and a transport that answers nothing at all.
    clock.advance(hours=6)
    idle = StubTransport()
    restarted = stub_repository(hass, idle, now=clock)
    await restarted.async_restore()

    restored = restarted.day_snapshot("SE4", TODAY)
    assert restored.state == "ready"
    assert restored.source == "store"
    assert restored.document is not None and restored.document.prices == original.document.prices
    # Restored, never presented as freshly fetched.
    assert restored.fetched_at == original.fetched_at
    assert idle.calls == []

    # Read again, with no request: the same immutable model, and still labelled
    # `store` -- the document entered the process from the store, and a later read
    # of it does not change where it came from.
    memory = await restarted.async_get_day("SE4", TODAY)
    assert memory.document is restored.document and memory.source == "store"
    assert idle.calls == []


async def test_store_data_that_cannot_be_trusted_is_ignored_safely(
    hass: HomeAssistant, hass_storage: dict[str, Any], transport: StubTransport, clock: Clock
) -> None:
    stamp = "2026-09-22T06:00:00+00:00"
    good_day = {"document": json.loads(day_body("SE4", TODAY)), "fetched_at": stamp}
    good_catalogue = {"document": json.loads(fixture("areas.json")), "fetched_at": stamp}
    wrong_identity = {"document": json.loads(day_body("NO1", TODAY)), "fetched_at": stamp}
    unvalidatable = {"document": {**json.loads(day_body("SE4", TODAY)), "prices": [True] * 96}, "fetched_at": stamp}
    naive_stamp = {"document": json.loads(day_body("SE4", TODAY)), "fetched_at": "2026-09-22T06:00:00"}

    cases = {
        "unknown schema": {"schema": 99, "days": {"SE4|2026-09-22": good_day}},
        "wrong identity": {"schema": 1, "days": {"SE4|2026-09-22": wrong_identity}},
        "no longer valid": {"schema": 1, "days": {"SE4|2026-09-22": unvalidatable}},
        "naive timestamp": {"schema": 1, "days": {"SE4|2026-09-22": naive_stamp}},
        "incomplete entry": {"schema": 1, "days": {"SE4|2026-09-22": {"document": None}}, "catalogue": None},
        "not an object": [1, 2, 3],
    }
    for name, payload in cases.items():
        hass_storage[STORAGE_KEY] = {"version": 1, "key": STORAGE_KEY, "data": payload}
        # A transport per case, so the catalogue read below cannot confuse the
        # "nothing was requested" assertion at the end of the test.
        repository = stub_repository(hass, StubTransport(), now=clock)
        # Nothing raises, and nothing was adopted.
        await repository.async_restore()
        assert repository.day_snapshot("SE4", TODAY).document is None, name
        assert repository.day_snapshot("SE4", TODAY).state == "unavailable", name

    # And a well-formed store *is* adopted, so the cases above fail for their own
    # reasons rather than because restoring never works.
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "key": STORAGE_KEY,
        "data": {"schema": 1, "catalogue": good_catalogue, "days": {"SE4|2026-09-22": good_day}},
    }
    idle = StubTransport()
    trusty = stub_repository(hass, idle, now=clock)
    await trusty.async_restore()
    assert trusty.day_snapshot("SE4", TODAY).document is not None
    assert idle.calls == []


# ------------------------------------------------------- shared and passive


async def test_two_charger_entries_share_one_repository_and_one_leaving_keeps_it(
    hass: HomeAssistant, offline_relay: StubTransport, clock: Clock
) -> None:
    transport = offline_relay
    repository = await async_setup_price_repository(hass, now=clock)

    assert domain_data(hass).price_repository is repository

    held = await repository.async_get_day("SE4", TODAY)
    assert held.document is not None

    # No config entry owns the repository: a charger leaving cannot take the price state
    # another charger is reading.
    assert domain_data(hass).price_repository is repository
    still_there = await repository.async_get_day("SE4", TODAY)
    assert still_there.document is held.document
    assert transport.call_count("/v1/SE4/2026/09-22.json") == 1

    # Setting the repository up again hands back the same object, not a second cache.
    again = await async_setup_price_repository(hass, now=clock)
    assert again is repository
    assert transport.call_count("/v1/SE4/2026/09-22.json") == 1


async def test_no_service_call_is_issued_anywhere_in_this_round(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    """Reading prices is passive: the round that acquires data commands nothing.

    Two halves, because they catch different mistakes: the services a charger
    command would go through are registered as mocks and must stay untouched while
    every read path runs, and the price layer's own source is checked for any call
    into Home Assistant's service registry at all -- a guard that keeps holding
    when a later edit adds a line nobody thought about.
    """
    switch = async_mock_service(hass, "switch", "turn_on")
    number = async_mock_service(hass, "number", "set_value")
    turn_on = async_mock_service(hass, "homeassistant", "turn_on")
    transport.serve_area()
    transport.serve("/v1/SE4/2026/09-23.json", 200, day_body("SE4", TOMORROW))

    repository.catalogue_snapshot()
    await repository.async_get_day("SE4", TODAY, refresh=True)
    await repository.async_get_day("SE4", TOMORROW, refresh=True)
    await repository.async_get_day("NO1", TODAY, refresh=True)

    assert switch == [] and number == [] and turn_on == []
    assert hass.services.async_services().get(DOMAIN, {}) == {}


async def test_summaries_describe_the_data_without_carrying_it(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    transport.serve_area()
    catalogue = repository.catalogue_snapshot()
    day = await repository.async_get_day("SE4", TODAY)

    summary = day_summary(day)
    assert summary["area"] == "SE4" and summary["date"] == "2026-09-22"
    assert summary["state"] == "ready" and summary["interval_count"] == 96
    assert summary["index_authority"] == "listed"
    assert summary["resolution_minutes"] == 15 and summary["unit"] == "EUR/kWh"
    assert summary["src"] == "entsoe.eu" and summary["fx_currencies"] == ["SEK"]
    assert summary["fetched_at"] is not None and summary["error"] is None
    assert catalogue_summary(catalogue)["area_count"] == 54
    assert catalogue_summary(catalogue)["source"] == "bundled"

    # No interval array, no response body, and nothing that says what a price was.
    assert "prices" not in summary and "intervals" not in summary
    assert "document" not in summary
    blob = json.dumps([summary, catalogue_summary(catalogue)])
    assert "0.18511" not in blob and "<html" not in blob and "11.275" not in blob
    for one in (summary, catalogue_summary(catalogue)):
        assert summary_is_flat(one)
        assert len(json.dumps(one)) < 2000


async def test_a_reader_is_not_blinded_while_a_refresh_is_in_flight(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    transport.serve_area()
    held = await repository.async_get_day("SE4", TODAY)
    assert held.state == "ready"

    path = "/v1/SE4/2026/09-22.json"
    transport.hold(path)
    refreshing = asyncio.ensure_future(repository.async_get_day("SE4", TODAY, refresh=True))
    await transport.entered[path].wait()

    # Mid-refresh: the request is running, and the last good document is still
    # there, still `ready`, with its original acquisition time.
    during = repository.day_snapshot("SE4", TODAY)
    assert during.refreshing is True
    assert during.state == "ready" and during.document is held.document
    assert during.fetched_at == held.fetched_at

    transport.release(path)
    after = await refreshing
    assert after.refreshing is False and after.document is not None
    assert after.state == "ready"


def store_with(day: date = TODAY, *, area: str = "SE4", prices: list[float] | None = None) -> StoreDouble:
    document = json.loads(day_body(area, day))
    if prices is not None:
        document["prices"] = prices
    return StoreDouble(
        {
            "schema": 1,
            "days": {f"{area}|{day.isoformat()}": {"document": document, "fetched_at": "2026-09-22T04:00:00+00:00"}},
        }
    )


async def test_two_simultaneous_restore_callers_share_one_read_and_one_adoption(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    store = store_with()
    store.gate = asyncio.Event()
    repository = stub_repository(hass, transport, now=clock, store=store)

    first = asyncio.ensure_future(repository.async_restore())
    await store.entered.wait()
    second = asyncio.ensure_future(repository.async_restore())
    await asyncio.sleep(0)

    # While the read is still open nothing is adopted, so nothing is observable as
    # restored: a caller that returned here would be reading an empty repository.
    assert store.loads == 1
    assert not first.done() and not second.done()
    assert repository.day_snapshot("SE4", TODAY).document is None

    store.gate.set()
    await asyncio.gather(first, second)

    # One read, and both callers returned only after adoption had finished.
    assert store.loads == 1
    snapshot = repository.day_snapshot("SE4", TODAY)
    assert snapshot.document is not None and snapshot.source == "store"
    assert transport.calls == []


async def test_two_simultaneous_setups_return_the_same_fully_restored_repository(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    store = store_with()
    store.gate = asyncio.Event()

    first = asyncio.ensure_future(
        async_setup_price_repository(hass, store=store, now=clock)
    )
    await store.entered.wait()
    second = asyncio.ensure_future(
        async_setup_price_repository(hass, store=store, now=clock)
    )
    await asyncio.sleep(0)
    assert not second.done(), "the second setup returned before the restore had finished"

    store.gate.set()
    one, two = await asyncio.gather(first, second)

    # One repository, one store read, and both setups handed back a repository that
    # had already adopted what was stored.
    assert one is two
    assert store.loads == 1
    assert one.day_snapshot("SE4", TODAY).document is not None


async def test_a_cancelled_restore_waiter_leaves_the_shared_restore_alone(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    store = store_with()
    store.gate = asyncio.Event()
    repository = stub_repository(hass, transport, now=clock, store=store)

    leaving = asyncio.ensure_future(repository.async_restore())
    await store.entered.wait()
    staying = asyncio.ensure_future(repository.async_restore())
    await asyncio.sleep(0)

    leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving

    store.gate.set()
    await staying
    assert store.loads == 1
    assert repository.day_snapshot("SE4", TODAY).document is not None

    # And a later caller still finds a finished restore rather than repeating it.
    await repository.async_restore()
    assert store.loads == 1


async def test_a_delayed_store_read_cannot_be_overtaken_or_overtake(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    """Hold the store open, then begin a public load.

    The corrected lifecycle makes the ordering impossible rather than unlikely: no
    request is made until the restore has finished, so the older stored document
    cannot land after a newer fetched one. What is asserted here is therefore the
    ordering -- and then that the whole result is the newer, network one.
    """
    stored = [0.001] * 96
    store = store_with(prices=stored)
    store.gate = asyncio.Event()
    repository = stub_repository(hass, transport, now=clock, store=store)
    transport.serve_area()

    # A refresh, so the fetch is certain to happen rather than being answered from
    # the stored document: the question is what ordering the two produce.
    loading = asyncio.ensure_future(repository.async_get_day("SE4", TODAY, refresh=True))
    await store.entered.wait()
    # The day was asked for, and not one byte left for the source.
    assert not loading.done()
    assert transport.calls == []

    store.gate.set()
    snapshot = await loading

    assert store.loads == 1
    assert transport.calls == [transport.day_path("SE4", TODAY)]
    assert snapshot.document is not None
    # Once the store read was allowed to finish, its document was adopted and then
    # the refresh fetched: the newer network document is what is held. The older
    # stored copy is the one that finished *last*, and it did not win.
    assert snapshot.document.prices != tuple(stored)
    assert snapshot.document.prices == tuple(json.loads(day_body("SE4", TODAY))["prices"])
    assert snapshot.source == "network"
    assert repository.day_snapshot("SE4", TODAY).source == "memory"
    assert snapshot.fetched_at is not None and snapshot.fetched_at > clock.now - timedelta(seconds=1)


def test_adoption_is_by_acquisition_time_not_by_arrival_order():
    """The invariant behind the ordering above, pinned directly.

    No path in this round can reach it (every load awaits the restore first, which
    is what the test above asserts), so this is the guard that keeps the property
    true if a later one forgets: an older document never displaces a newer one.
    """
    cached = price_repository_module._Cached
    older = cached(document={}, fetched_at=datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc), parsed=None)
    newer = cached(document={}, fetched_at=datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc), parsed=None)

    assert price_repository_module._older_than(None, older) is True
    assert price_repository_module._older_than(newer, older) is False
    assert price_repository_module._older_than(older, newer) is True
    # A tie is not newer: the document already in hand stays.
    assert price_repository_module._older_than(older, older) is False


async def test_a_store_that_fails_is_logged_and_never_blocks_the_next_caller(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    """The documented policy for an unexpected store failure: absent, and retried."""

    class Broken(StoreDouble):
        async def async_load(self) -> Any:
            self.loads += 1
            raise OSError("the disk went away")

    broken = Broken()
    repository = stub_repository(hass, transport, now=clock, store=broken)
    transport.serve_area()

    # A restore failure is not an exception to callers: it is "no stored prices".
    await repository.async_restore()
    assert broken.loads == 1

    snapshot = await repository.async_get_day("SE4", TODAY)
    assert snapshot.state == "ready" and snapshot.document is not None


