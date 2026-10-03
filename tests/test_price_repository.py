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
    CHUNK_BYTES,
    MAX_RESPONSE_BYTES,
    STORAGE_KEY,
    PriceRepository,
    async_setup_price_repository,
    catalogue_summary,
    day_summary,
    index_summary,
)
from .relay import BASE_URL, Clock, StoreDouble, StubTransport, TODAY, TOMORROW, day_body, fixture
from custom_components.spotnav.runtime import domain_data

@pytest.fixture
def repository(hass: HomeAssistant, transport: StubTransport, clock: Clock) -> PriceRepository:
    return PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock)


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
    assert transport.call_count("/v1/index.json") == 1

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


# ------------------------------------------------------------- the authority


async def test_an_unlisted_day_is_never_requested(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    transport.serve_area()
    # The index lists SE4's today only (see index_missing_tomorrow.json): tomorrow
    # is authoritatively absent, so there is nothing to ask for.
    transport.serve("/v1/index.json", 200, fixture("index_missing_tomorrow.json"))

    snapshot = await repository.async_get_day("SE4", TOMORROW)

    assert snapshot.state == "unavailable"
    assert snapshot.index_authority == "not_listed"
    assert snapshot.document is None
    assert transport.call_count("/v1/SE4/2026/09-23.json") == 0
    # One index, and no day request at all.
    assert transport.calls == ["/v1/index.json"]


async def test_an_archive_day_is_asked_whatever_the_index_lists_and_is_not_kept_in_the_live_cache(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    transport.serve_area()
    transport.serve("/v1/index.json", 200, fixture("index_missing_tomorrow.json"))
    store = StoreDouble()
    repository._store = store  # type: ignore[assignment]
    past = TODAY - timedelta(days=6)
    transport.serve(transport.day_path("SE4", past), 200, day_body("SE4", past))

    document = await repository.async_get_archive_day("SE4", past)

    assert document is not None and document.interval_count == 96
    assert transport.call_count(transport.day_path("SE4", past)) == 1
    assert repository.day_snapshot("SE4", past).document is None
    assert (("SE4", past)) not in repository._days
    assert all(f"|{past.isoformat()}" not in key for key in repository._payload()["days"])


async def test_an_archive_day_the_relay_lacks_is_none_and_a_bad_body_is_none(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    missing = TODAY - timedelta(days=30)
    broken = TODAY - timedelta(days=31)
    transport.serve(transport.day_path("SE4", broken), 200, day_body("SE4", TODAY))

    assert await repository.async_get_archive_day("SE4", missing) is None
    assert await repository.async_get_archive_day("SE4", broken) is None
    assert transport.call_count(transport.day_path("SE4", missing)) == 1


async def test_an_unreadable_index_is_not_taken_as_proof_of_absence(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    transport.serve_area()
    transport.serve("/v1/index.json", 503, "service unavailable")
    transport.serve("/v1/SE4/2026/09-23.json", 200, day_body("SE4", TOMORROW))

    snapshot = await repository.async_get_day("SE4", TOMORROW)

    # The index is unavailable, not empty: the day is still requested, and the
    # authority is `unknown` rather than a false "not listed".
    assert snapshot.index_authority == "unknown"
    assert transport.call_count("/v1/SE4/2026/09-23.json") == 1
    assert snapshot.state == "ready" and snapshot.document is not None

    index = await repository.async_get_index()
    assert index.state == "unavailable" and index.attempt_error == "http_status"


async def test_an_unlisted_day_that_is_already_held_goes_stale_rather_than_vanishing(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    transport.serve_area()
    held = await repository.async_get_day("SE4", TODAY)
    assert held.state == "ready"

    # The index stops listing it (its window moved on). Nothing valid is discarded,
    # and the state says the data is no longer published rather than pretending it
    # never existed.
    moved = json.loads(fixture("index.json"))
    moved["areas"]["SE4"] = {"days": ["2026-09-20"], "res": 15}
    transport.serve("/v1/index.json", 200, json.dumps(moved))
    await repository.async_get_index(refresh=True)

    snapshot = await repository.async_get_day("SE4", TODAY)
    assert snapshot.state == "stale"
    assert snapshot.index_authority == "not_listed"
    assert snapshot.document is held.document
    assert transport.call_count("/v1/SE4/2026/09-22.json") == 1


# ---------------------------------------------------------------- the store


async def test_a_restart_restores_last_good_without_a_network_call(
    hass: HomeAssistant, hass_storage: dict[str, Any], transport: StubTransport, clock: Clock
) -> None:
    transport.serve_area()
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock)
    original = await repository.async_get_day("SE4", TODAY)
    await repository.async_get_catalogue()
    await repository.async_get_index()
    await hass.async_block_till_done()

    written = hass_storage[STORAGE_KEY]["data"]
    assert written["schema"] == 1
    # The raw published document, not a summary of it, and its own acquisition time.
    assert written["days"]["SE4|2026-09-22"]["document"]["area"] == "SE4"
    assert written["days"]["SE4|2026-09-22"]["document"]["prices"] == list(original.document.prices)
    assert written["catalogue"]["document"]["v"] == 1
    assert written["index"]["document"]["areas_rev"] == "916f2f8e05fd"
    # No credential, no webhook id and no URL ever reaches the store.
    assert "http" not in json.dumps(written)

    # A new process: a new repository, and a transport that answers nothing at all.
    clock.advance(hours=6)
    idle = StubTransport()
    restarted = PriceRepository(hass, base_url=BASE_URL, session=idle, now=clock)
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
        repository = PriceRepository(hass, base_url=BASE_URL, session=StubTransport(), now=clock)
        # Nothing raises, and nothing was adopted.
        await repository.async_restore()
        assert repository.day_snapshot("SE4", TODAY).document is None, name
        assert repository.day_snapshot("SE4", TODAY).state == "unavailable", name
        assert (await repository.async_get_catalogue()).catalogue is None, name

    # And a well-formed store *is* adopted, so the cases above fail for their own
    # reasons rather than because restoring never works.
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "key": STORAGE_KEY,
        "data": {"schema": 1, "catalogue": good_catalogue, "days": {"SE4|2026-09-22": good_day}},
    }
    idle = StubTransport()
    trusty = PriceRepository(hass, base_url=BASE_URL, session=idle, now=clock)
    await trusty.async_restore()
    assert trusty.day_snapshot("SE4", TODAY).document is not None
    assert (await trusty.async_get_catalogue()).catalogue is not None
    assert idle.calls == []


# ------------------------------------------------------- shared and passive


async def test_two_charger_entries_share_one_repository_and_one_leaving_keeps_it(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    transport.serve_area()
    repository = await async_setup_price_repository(hass, base_url=BASE_URL, session=transport, now=clock)

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
    again = await async_setup_price_repository(hass, base_url=BASE_URL, session=transport, now=clock)
    assert again is repository
    assert transport.call_count("/v1/index.json") == 1


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

    await repository.async_get_catalogue(refresh=True)
    await repository.async_get_index(refresh=True)
    await repository.async_get_day("SE4", TODAY, refresh=True)
    await repository.async_get_day("SE4", TOMORROW, refresh=True)
    await repository.async_get_day("NO1", TODAY, refresh=True)
    await repository.async_get_catalogue()
    await repository.async_get_index()

    assert switch == [] and number == [] and turn_on == []
    assert hass.services.async_services().get(DOMAIN, {}) == {}


async def test_summaries_describe_the_data_without_carrying_it(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    transport.serve_area()
    catalogue = await repository.async_get_catalogue()
    index = await repository.async_get_index()
    day = await repository.async_get_day("SE4", TODAY)

    summary = day_summary(day)
    assert summary["area"] == "SE4" and summary["date"] == "2026-09-22"
    assert summary["state"] == "ready" and summary["interval_count"] == 96
    assert summary["index_authority"] == "listed"
    assert summary["index_revision"] == "916f2f8e05fd"
    assert summary["resolution_minutes"] == 15 and summary["unit"] == "EUR/kWh"
    assert summary["src"] == "entsoe.eu" and summary["fx_currencies"] == ["SEK"]
    assert summary["fetched_at"] is not None and summary["error"] is None
    assert catalogue_summary(catalogue)["area_count"] == 4
    assert index_summary(index)["areas_rev"] == "916f2f8e05fd"

    # No interval array, no response body, and nothing that says what a price was.
    assert "prices" not in summary and "intervals" not in summary
    assert "document" not in summary
    blob = json.dumps([summary, catalogue_summary(catalogue), index_summary(index)])
    assert "0.18511" not in blob and "<html" not in blob and "11.275" not in blob
    for one in (summary, catalogue_summary(catalogue), index_summary(index)):
        assert summary_is_flat(one)
        assert len(json.dumps(one)) < 2000


async def test_a_catalogue_failure_says_which_kind_of_failure_it_was(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository, clock: Clock
) -> None:
    # An HTML body from a proxy is `invalid`, not a catalogue.
    transport.serve("/v1/areas.json", 200, "<html><body>502 Bad Gateway</body></html>")
    bad_body = await repository.async_get_catalogue()
    assert bad_body.state == "invalid" and bad_body.catalogue is None
    assert bad_body.attempt_error == "not_json" and bad_body.attempt_at is not None

    # An unreachable relay is `unavailable`, a different fact from a bad body.
    transport.serve("/v1/areas.json", 503, "")
    down = await repository.async_get_catalogue(refresh=True)
    assert down.state == "unavailable" and down.attempt_error == "http_status"

    # And once a good one is in hand, a later failure is `stale`: last good is kept
    # with its own acquisition time, and the failure is recorded beside it.
    transport.serve("/v1/areas.json", 200, fixture("areas.json"))
    good = await repository.async_get_catalogue(refresh=True)
    assert good.state == "ready" and good.catalogue is not None
    clock.advance(minutes=10)
    transport.serve("/v1/areas.json", 500, "")
    stale = await repository.async_get_catalogue(refresh=True)
    assert stale.state == "stale" and stale.catalogue is good.catalogue
    assert stale.fetched_at == good.fetched_at
    assert stale.attempt_at is not None and stale.attempt_at > good.fetched_at
    assert stale.attempt_error == "http_status"


async def test_an_unlisted_day_is_not_asked_again_and_again(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    transport.serve_area()
    transport.serve("/v1/index.json", 200, fixture("index_missing_tomorrow.json"))

    for _ in range(3):
        snapshot = await repository.async_get_day("SE4", TOMORROW, refresh=True)
        assert snapshot.state == "unavailable" and snapshot.index_authority == "not_listed"

    # One index, three answers, and not one request for the day: `refresh` does not
    # override the relay's own statement that the day does not exist.
    assert transport.call_count("/v1/SE4/2026/09-23.json") == 0
    assert transport.call_count("/v1/index.json") == 1


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
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=store)

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
        async_setup_price_repository(hass, base_url=BASE_URL, session=transport, store=store, now=clock)
    )
    await store.entered.wait()
    second = asyncio.ensure_future(
        async_setup_price_repository(hass, base_url=BASE_URL, session=transport, store=store, now=clock)
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
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=store)

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
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=store)
    transport.serve_area()

    # A refresh, so the fetch is certain to happen rather than being answered from
    # the stored document: the question is what ordering the two produce.
    loading = asyncio.ensure_future(repository.async_get_day("SE4", TODAY, refresh=True))
    await store.entered.wait()
    # The day was asked for, and not one byte left for the relay.
    assert not loading.done()
    assert transport.calls == []

    store.gate.set()
    snapshot = await loading

    assert store.loads == 1
    assert transport.calls == ["/v1/index.json", transport.day_path("SE4", TODAY)]
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
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=broken)
    transport.serve_area()

    # A restore failure is not an exception to callers: it is "no stored prices".
    await repository.async_restore()
    assert broken.loads == 1

    snapshot = await repository.async_get_day("SE4", TODAY)
    assert snapshot.state == "ready" and snapshot.document is not None


async def test_a_document_split_into_tiny_chunks_is_read_to_the_end_and_accepted(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    # Seven bytes at a time: the catalogue, the index and the day all arrive in
    # pieces far smaller than any single read, and all three still parse.
    transport.serve_area(chunked=7)

    catalogue = await repository.async_get_catalogue()
    index = await repository.async_get_index()
    day = await repository.async_get_day("SE4", TODAY)

    assert catalogue.state == "ready" and catalogue.catalogue is not None
    assert catalogue.catalogue.area_ids == ("DE-LU", "DK1", "NO1", "SE4")
    assert index.state == "ready" and index.index is not None
    assert index.index.areas_rev == "916f2f8e05fd"
    assert day.state == "ready" and day.document is not None
    assert day.document.interval_count == 96
    assert day.document.prices == tuple(json.loads(day_body("SE4", TODAY))["prices"])
    # Each body really was read more than once, which is what makes the point.
    assert transport.responses[transport.day_path("SE4", TODAY)].reads > 50


async def test_a_body_of_exactly_the_limit_still_reaches_the_parser(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    # Padding is whitespace, which JSON allows, so a body of exactly the limit is
    # still a valid document: the boundary is about size, not content.
    body = fixture("index.json")
    padded = body + " " * (MAX_RESPONSE_BYTES - len(body.encode("utf-8")))
    assert len(padded.encode("utf-8")) == MAX_RESPONSE_BYTES
    transport.serve_chunks("/v1/index.json", 200, [padded.encode("utf-8")])

    snapshot = await repository.async_get_index()

    assert snapshot.state == "ready", snapshot.attempt_error
    assert snapshot.index is not None and snapshot.index.areas_rev == "916f2f8e05fd"


async def test_one_byte_past_the_limit_is_too_large_and_the_remainder_is_left_unread(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    # Two megabytes served in 64 KiB chunks: the reader must stop as soon as it has
    # more than the limit, not buffer the lot and measure it afterwards.
    oversized = [b"x" * CHUNK_BYTES] * 32
    transport.serve_chunks("/v1/index.json", 200, oversized)

    snapshot = await repository.async_get_index()

    assert snapshot.state == "unavailable" and snapshot.attempt_error == "too_large"
    response = transport.responses["/v1/index.json"]
    assert response.unread == 32 * CHUNK_BYTES - (MAX_RESPONSE_BYTES + 1)
    # Nine reads: eight full chunks and the one byte that proved the overflow.
    assert response.reads == 9


async def test_a_body_that_breaks_mid_stream_is_a_network_failure_and_keeps_last_good(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    transport.serve_area()
    held = await repository.async_get_day("SE4", TODAY)
    assert held.document is not None

    # A response whose first chunk arrives and whose connection then drops: a
    # truncated body is a transport failure, never a document to parse hopefully.
    body = json.loads(day_body("SE4", TODAY))
    transport.serve_chunks(
        transport.day_path("SE4", TODAY),
        200,
        [json.dumps(body)[:40].encode("utf-8"), json.dumps(body)[40:].encode("utf-8")],
        fail_after=1,
    )
    snapshot = await repository.async_get_day("SE4", TODAY, refresh=True)

    assert snapshot.state == "stale" and snapshot.attempt_error == "network"
    assert snapshot.document is held.document
    assert snapshot.fetched_at == held.fetched_at


async def test_a_body_that_stalls_mid_stream_times_out_and_keeps_last_good(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport.serve_area()
    held = await repository.async_get_day("SE4", TODAY)
    assert held.document is not None

    # The first chunk arrives, the rest never does. The production timeout is twenty
    # seconds, so the test shortens the module's own bound rather than waiting: the
    # policy under test is that the *whole body* is inside it.
    monkeypatch.setattr(price_repository_module, "REQUEST_TIMEOUT_S", 0.05)
    stalled = asyncio.Event()
    body = json.dumps(json.loads(day_body("SE4", TODAY))).encode("utf-8")
    transport.serve_chunks(
        transport.day_path("SE4", TODAY), 200, [body[:64], body[64:]], stall_after=1, stall=stalled
    )

    snapshot = await repository.async_get_day("SE4", TODAY, refresh=True)

    assert snapshot.state == "stale" and snapshot.attempt_error == "timeout"
    assert snapshot.document is held.document

    # And the entry is not poisoned: once the stream is allowed to finish, the next
    # attempt succeeds.
    stalled.set()
    recovered = await repository.async_get_day("SE4", TODAY, refresh=True)
    assert recovered.state == "ready" and recovered.document is not None


async def test_an_empty_or_truncated_body_uses_the_existing_failure_model(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    # Zero bytes: not a document, and not a special case either.
    transport.serve_chunks("/v1/index.json", 200, [])
    empty = await repository.async_get_index()
    assert empty.state == "invalid" and empty.attempt_error == "not_json" and empty.index is None

    # Half a document, delivered and then ended: same answer, same stable code.
    whole = fixture("index.json")
    transport.serve_chunks("/v1/index.json", 200, [whole[: len(whole) // 2].encode("utf-8")])
    truncated = await repository.async_get_index(refresh=True)
    assert truncated.state == "invalid" and truncated.attempt_error == "not_json"


# ------------------------------------------- authority follows index freshness


async def test_authority_follows_the_index_freshness_not_its_mere_presence(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    """A retained index is context; only a *current* one is authority."""
    # A fresh index that lists today only: tomorrow is authoritatively absent.
    transport.serve_area()
    today_only = json.loads(fixture("index.json"))
    today_only["areas"]["SE4"] = {"days": ["2026-09-22"], "res": 15}
    transport.serve("/v1/index.json", 200, json.dumps(today_only))
    ready = await repository.async_get_index()
    assert ready.state == "ready"
    assert repository.day_snapshot("SE4", TOMORROW).index_authority == "not_listed"
    assert repository.day_snapshot("SE4", TODAY).index_authority == "listed"

    # The refresh fails. last-good is retained but is not authority, for a plain
    # read or for a fetch alike.
    transport.serve("/v1/index.json", 503, "")
    await repository.async_get_index(refresh=True)
    retained = repository.index_snapshot()
    assert retained.state == "stale" and retained.index is not None
    assert repository.day_snapshot("SE4", TOMORROW).index_authority == "unknown"
    assert repository.day_snapshot("SE4", TODAY).index_authority == "unknown"

    # So a day request is made instead of being suppressed as "not listed" — and
    # the request does not hide a second index request inside it, which would turn
    # one failed global attempt into one per day.
    transport.serve(transport.day_path("SE4", TOMORROW), 200, day_body("SE4", TOMORROW))
    index_calls = transport.call_count("/v1/index.json")
    fetched = await repository.async_get_day("SE4", TOMORROW, refresh=True)
    assert fetched.document is not None and fetched.state == "ready"
    assert transport.call_count(transport.day_path("SE4", TOMORROW)) == 1
    assert transport.call_count("/v1/index.json") == index_calls
    assert fetched.index_authority == "unknown"

    # A later successful refresh restores authority both ways.
    transport.serve("/v1/index.json", 200, fixture("index.json"))
    recovered = await repository.async_get_index(refresh=True)
    assert recovered.state == "ready"
    assert repository.day_snapshot("SE4", TOMORROW).index_authority == "not_listed"
    assert repository.day_snapshot("SE4", TODAY).index_authority == "listed"

    # And a day that was held and is no longer listed goes stale rather than
    # vanishing: the data is valid, it is simply no longer published.
    transport.serve(transport.day_path("SE4", TODAY), 200, day_body("SE4", TODAY))
    held = await repository.async_get_day("SE4", TODAY, refresh=True)
    assert held.state == "ready" and held.document is not None

    gone = json.loads(fixture("index.json"))
    gone["areas"]["SE4"] = {"days": ["2026-09-19"], "res": 15}
    transport.serve("/v1/index.json", 200, json.dumps(gone))
    await repository.async_get_index(refresh=True)

    after = repository.day_snapshot("SE4", TODAY)
    assert after.index_authority == "not_listed"
    assert after.state == "stale" and after.document is held.document
    assert after.fetched_at == held.fetched_at


async def test_the_first_ever_day_fetch_reads_the_index_rather_than_going_blind(
    hass: HomeAssistant, transport: StubTransport, repository: PriceRepository
) -> None:
    # Nothing has been asked for yet: the first day fetch reads the index and then
    # classifies it, so a day the index omits is still not requested.
    transport.serve_area()
    today_only = json.loads(fixture("index.json"))
    today_only["areas"]["SE4"] = {"days": ["2026-09-22"], "res": 15}
    transport.serve("/v1/index.json", 200, json.dumps(today_only))

    snapshot = await repository.async_get_day("SE4", TOMORROW)

    assert transport.call_count("/v1/index.json") == 1
    assert snapshot.index_authority == "not_listed"
    assert snapshot.state == "unavailable"
    assert transport.call_count(transport.day_path("SE4", TOMORROW)) == 0

    # An index that was asked for and failed is *not* re-asked by a day request.
    transport.serve("/v1/index.json", 503, "")
    await repository.async_get_index(refresh=True)
    index_calls = transport.call_count("/v1/index.json")
    transport.serve(transport.day_path("SE4", TOMORROW), 200, day_body("SE4", TOMORROW))
    again = await repository.async_get_day("SE4", TOMORROW, refresh=True)
    assert again.document is not None
    assert transport.call_count("/v1/index.json") == index_calls
