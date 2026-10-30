"""ENTSO-E with the installation's own key, over a fake aiohttp session (no network, the platform is never asked).

The fake answers by zone and period as the platform would: an A44 document, an acknowledgement, a status with a
`Retry-After`, or a transport failure whose text holds the whole request URL (as aiohttp's do). The relay wire is
the suite's stub and the exchange rates a fixed table.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.pricing.area_catalogue import async_load_bundled
from custom_components.spotnav.pricing.entsoe_parse import EntsoeToken, market_day_bounds
from custom_components.spotnav.pricing.price_refresh import PriceRefreshManager
from custom_components.spotnav.pricing.price_repository import PriceRepository
from custom_components.spotnav.pricing.price_store import PriceSourceStore
from custom_components.spotnav.pricing.providers import choose_provider, DayFetch, ProviderRouter
from custom_components.spotnav.pricing.providers.entsoe import EntsoeProvider, PUBLICATION
from custom_components.spotnav.pricing.providers.nordpool import NordPoolProvider
from custom_components.spotnav.pricing.relay_contract import DEFAULT_PUBLICATION, parse_day

from .entsoe_helpers import area as entsoe_area, entsoe_body, fixture, SECRET, with_sequence
from .relay import Clock, day_body as relay_day_body, FakeScheduler, serve, StoreDouble, StubTransport

UTC = timezone.utc
#: The relay fixtures' day, before the publication (08:00 in Stockholm).
NOW = datetime(2026, 9, 22, 6, 0, tzinfo=UTC)
TODAY = date(2026, 9, 22)
TOMORROW = date(2026, 9, 23)
YESTERDAY = date(2026, 9, 21)
SEK_RATE = 11.0123
RATE_DAY = date(2026, 9, 21)


def day_body(area_id: str, day: date, *, base: float = 40.0) -> str:
    """The platform's A44 document for an area's CET market day, at quarter hours (EUR/MWh)."""
    area = entsoe_area(area_id)
    start, end = market_day_bounds(day, area.market_tz)
    slots = int((end - start).total_seconds() // 900)
    return entsoe_body(
        area.eic,
        start.strftime("%Y-%m-%dT%H:%MZ"),
        end.strftime("%Y-%m-%dT%H:%MZ"),
        "PT15M",
        [f"{base + i * 0.25:.2f}" for i in range(slots)],
    )


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers
        self.content = self
        self._body = body

    async def read(self, limit: int) -> bytes:
        chunk, self._body = self._body[:limit], self._body[limit:]
        return chunk


class FakeRequest:
    def __init__(self, platform: FakePlatform, url: str, headers: dict[str, str] | None) -> None:
        self._platform = platform
        self._url = url
        self._headers = headers or {}

    async def __aenter__(self) -> FakeResponse:
        return await self._platform.answer(self._url, self._headers)

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakePlatform:
    """The Transparency Platform behind Home Assistant's session: answers per (EIC, periodStart)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []
        self.headers: list[dict[str, str]] = []
        self.answers: dict[tuple[str, str], Any] = {}
        #: What every request gets when nothing else is served for it.
        self.fallback: Any = (200, fixture("synthetic_ack_999_no_data.xml"), {})

    def serve(self, area_id: str, day: date, answer: Any) -> None:
        area = entsoe_area(area_id)
        start, _ = market_day_bounds(day, area.market_tz)
        self.answers[(area.eic, start.strftime("%Y%m%d%H%M"))] = answer

    def serve_day(self, area_id: str, day: date, **kwargs: Any) -> None:
        self.serve(area_id, day, (200, day_body(area_id, day, **kwargs), {}))

    def get(self, url: str, headers: dict[str, str] | None = None) -> FakeRequest:
        return FakeRequest(self, url, headers)

    async def answer(self, url: str, headers: dict[str, str]) -> FakeResponse:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.requests.append(query)
        self.headers.append(headers)
        answer = self.answers.get((query["in_Domain"], query["periodStart"]), self.fallback)
        if callable(answer):
            answer = answer(url)
        if isinstance(answer, BaseException):
            raise answer
        status, body, extra = answer
        return FakeResponse(status, body.encode("utf-8") if isinstance(body, str) else body, extra)

    def asked(self) -> list[tuple[str, str]]:
        return [(query["in_Domain"], query["periodStart"]) for query in self.requests]


class FakeRates:
    def __init__(self) -> None:
        self.rates = {"SEK": SEK_RATE}
        self.refreshed = 0

    def rate_for(self, currency: str, day: date) -> tuple[float, date] | None:
        rate = self.rates.get(currency)
        return None if rate is None else (rate, RATE_DAY)

    async def async_start(self) -> None:
        return None

    async def async_refresh(self) -> bool:
        self.refreshed += 1
        return False


class World:
    def __init__(self, hass: HomeAssistant, transport: StubTransport) -> None:
        self.hass = hass
        self.clock = Clock(NOW)
        self.transport = transport
        self.platform = FakePlatform()
        self.rates = FakeRates()
        self.scheduler = FakeScheduler()
        self.cache = StoreDouble()
        self.nordpool_entry = MockConfigEntry(domain="nordpool", title="Nord Pool", data={})
        self.changes = 0

    async def start(self, *, key: str | None = SECRET, state: str = "ok") -> None:
        self.keys = PriceSourceStore(self.hass)
        await self.keys.async_load()
        if key is not None:
            await self.keys.async_set_entsoe_key(EntsoeToken(key), state)
        bundled = await async_load_bundled(self.hass)
        assert bundled is not None
        self.nordpool_entry.add_to_hass(self.hass)
        self.provider = EntsoeProvider(
            self.hass, bundled, self.keys, self.rates, session=self.platform, now=self.clock, min_spacing_s=0
        )
        self.router = ProviderRouter(
            NordPoolProvider(self.hass, bundled, self.rates, now=self.clock),
            self.provider,
        )
        self.router.async_start(self.hass)
        self.router.add_listener(self._changed)
        self.repository = PriceRepository(
            self.hass, now=self.clock, store=self.cache, bundled=bundled, router=self.router
        )
        self.manager = PriceRefreshManager(
            self.hass, self.repository, now=self.clock, jitter=lambda: 0.5, scheduler=self.scheduler
        )

    def _changed(self) -> None:
        self.changes += 1

    async def cycle(self, area: str) -> Any:
        snapshot = await self.manager.async_refresh_area(area)
        await self.hass.async_block_till_done()
        return snapshot

    async def stop(self) -> None:
        await self.manager.async_shutdown()
        self.router.async_stop()
        self.nordpool_entry.mock_state(self.hass, ConfigEntryState.NOT_LOADED)


@pytest.fixture
async def world(hass: HomeAssistant, transport: StubTransport) -> World:
    instance = World(hass, transport)
    yield instance
    if hasattr(instance, "manager"):
        await instance.stop()


def utc_period(area_id: str, day: date) -> tuple[str, str]:
    area = entsoe_area(area_id)
    start, _ = market_day_bounds(day, area.market_tz)
    return area.eic, start.strftime("%Y%m%d%H%M")


# ------------------------------------------------------------------ the document and the request


async def test_a_day_becomes_the_relays_day_document_from_one_request(world: World) -> None:
    await world.start()
    world.platform.serve_day("SE4", TODAY)

    fetch = await world.provider.async_fetch_day("SE4", TODAY)

    assert fetch.outcome == "fetched"
    [query] = world.platform.requests
    assert query == {
        "documentType": "A44",
        "contract_MarketAgreement.type": "A01",
        "periodStart": "202609212200",
        "periodEnd": "202609222200",
        "in_Domain": "10Y1001A1001A47J",
        "out_Domain": "10Y1001A1001A47J",
        "securityToken": SECRET,
    }
    assert world.platform.headers[0]["User-Agent"].startswith("SpotNav")
    document = fetch.document
    assert document["area"] == "SE4" and document["date"] == "2026-09-22"
    assert document["tz"] == "Europe/Stockholm" and "market_tz" not in document
    assert document["src"] == "entsoe.eu" and document["res"] == 15 and len(document["prices"]) == 96
    assert document["prices"][0] == 0.04 and document["prices"][1] == 0.04025
    assert document["fx"] == {"SEK": SEK_RATE} and document["fx_date"] == "2026-09-21" and document["fx_src"] == "ECB"
    assert document["published"] == "2026-09-18T14:31:00+02:00"
    parsed = parse_day(document, area_id="SE4", day=TODAY)
    assert parsed.covers_whole_day() and parsed.fx_rate("SEK") == SEK_RATE


async def test_a_euro_area_with_a_sequence_asks_for_it_and_needs_no_rate(world: World) -> None:
    await world.start()
    world.platform.serve("DE-LU", TODAY, (200, with_sequence(day_body("DE-LU", TODAY), "1"), {}))

    fetch = await world.provider.async_fetch_day("DE-LU", TODAY)

    assert fetch.outcome == "fetched"
    assert world.platform.requests[0]["classificationSequence_AttributeInstanceComponent.position"] == "1"
    assert fetch.document["fx"] == {} and "fx_date" not in fetch.document
    assert world.rates.refreshed == 0


async def test_finland_is_asked_for_cet_days_and_its_day_is_cut_from_two(world: World) -> None:
    await world.start()
    for day in (YESTERDAY, TODAY):
        world.platform.serve_day("FI", day)

    snapshot = await world.repository.async_get_day("FI", TODAY)

    # The CET day starts at 22:00 UTC in summer: the Brussels midnight, not Helsinki's.
    assert [period for _, period in world.platform.asked()] == ["202609202200", "202609212200"]
    assert snapshot.is_complete and snapshot.document.interval_count == 96
    assert [part.interval_count for part in snapshot.document.parts] == [4, 92]
    assert set(world.cache.payload["days"]) == {"FI|Europe/Brussels|2026-09-21", "FI|Europe/Brussels|2026-09-22"}
    assert world.cache.payload["days"]["FI|Europe/Brussels|2026-09-22"]["provider"] == "entsoe"
    assert world.repository.area_entry("FI").publication == PUBLICATION == DEFAULT_PUBLICATION


async def test_history_reaches_years_back(world: World) -> None:
    await world.start()
    old = date(2024, 2, 1)
    world.platform.serve_day("SE4", old)

    document = await world.repository.async_get_archive_day("SE4", old)

    assert document is not None and document.day == old
    assert world.provider.history_reach is None


def range_body(area_id: str, first: date, last: date, *, base: float = 40.0) -> str:
    """The platform's A44 answer for consecutive CET market days: one period over all of them, at quarter hours."""
    area = entsoe_area(area_id)
    start, _ = market_day_bounds(first, area.market_tz)
    _, end = market_day_bounds(last, area.market_tz)
    slots = int((end - start).total_seconds() // 900)
    return entsoe_body(
        area.eic,
        start.strftime("%Y-%m-%dT%H:%MZ"),
        end.strftime("%Y-%m-%dT%H:%MZ"),
        "PT15M",
        [f"{base + (i % 96) * 0.25:.2f}" for i in range(slots)],
    )


async def test_a_range_of_past_days_is_one_request_read_day_by_day(world: World) -> None:
    await world.start()
    first, last = date(2026, 3, 20), date(2026, 4, 2)  # across the spring clock change (29 March: 92 quarters)
    world.platform.serve("SE4", first, (200, range_body("SE4", first, last), {}))
    days = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]

    fetches = await world.provider.async_fetch_days("SE4", days)

    [query] = world.platform.requests
    assert query["periodStart"] == "202603192300" and query["periodEnd"] == "202604022200"
    assert sorted(fetches) == days and all(fetch.outcome == "fetched" for fetch in fetches.values())
    for day, fetch in fetches.items():
        parsed = parse_day(fetch.document, area_id="SE4", day=day)
        assert parsed.covers_whole_day() and fetch.document["src"] == "entsoe.eu"
    assert parse_day(fetches[date(2026, 3, 29)].document, area_id="SE4", day=date(2026, 3, 29)).interval_count == 92
    assert world.provider.history_range_days == 31


async def test_a_range_answer_missing_a_day_says_that_day_is_not_published(world: World) -> None:
    await world.start()
    first, last = date(2026, 9, 1), date(2026, 9, 3)
    # The answer covers the first two days only.
    world.platform.serve("SE4", first, (200, range_body("SE4", first, date(2026, 9, 2)), {}))

    fetches = await world.provider.async_fetch_days("SE4", [first, date(2026, 9, 2), last])

    assert [fetches[day].outcome for day in sorted(fetches)] == ["fetched", "fetched", "not_published"]
    assert len(world.platform.requests) == 1


async def test_a_rejected_key_on_a_range_is_recorded_once_for_every_day(world: World) -> None:
    await world.start()
    first = date(2026, 9, 1)
    world.platform.serve("SE4", first, (401, "", {}))
    marks: list[str] = []
    original = world.keys.async_mark_entsoe_key

    async def mark(token, state):
        marks.append(state)
        return await original(token, state)

    world.keys.async_mark_entsoe_key = mark  # type: ignore[method-assign]
    days = [first + timedelta(days=offset) for offset in range(5)]

    fetches = await world.provider.async_fetch_days("SE4", days)

    assert {fetch.code for fetch in fetches.values()} == {"entsoe_auth"} and marks == ["rejected"]
    assert len(world.platform.requests) == 1


async def test_a_range_must_be_consecutive_and_at_most_a_month(world: World) -> None:
    await world.start()
    with pytest.raises(ValueError):
        await world.provider.async_fetch_days("SE4", [date(2026, 9, 1), date(2026, 9, 3)])
    with pytest.raises(ValueError):
        await world.provider.async_fetch_days("SE4", [date(2026, 8, 1) + timedelta(days=n) for n in range(32)])
    assert world.platform.requests == []


# ------------------------------------------------------------------ the schedule


async def test_tomorrow_is_not_asked_before_the_window_and_no_data_is_not_published(world: World) -> None:
    await world.start()
    world.platform.serve_day("SE4", TODAY)
    await world.manager.async_subscribe(owner_id="charger", area_id="SE4")
    await world.hass.async_block_till_done()

    assert world.platform.asked() == [utc_period("SE4", TODAY)]
    snapshot = world.manager.area_snapshot("SE4")
    assert snapshot.today_state == "ready" and snapshot.waiting_for_tomorrow
    assert world.transport.calls == []

    world.clock.now = datetime(2026, 9, 22, 10, 56, tzinfo=UTC)  # 12:56 in Brussels: the window is open
    world.platform.serve("SE4", TOMORROW, (200, fixture("synthetic_ack_999_no_data.xml"), {}))
    snapshot = await world.cycle("SE4")
    assert world.platform.asked()[-1] == utc_period("SE4", TOMORROW)
    assert snapshot.tomorrow_authority == "not_listed" and "SE4|2026-09-23" not in str(world.cache.payload)

    world.platform.serve_day("SE4", TOMORROW)
    world.clock.now += timedelta(minutes=5)
    snapshot = await world.cycle("SE4")
    assert snapshot.state == "ready" and snapshot.tomorrow_snapshot.is_complete
    requests = len(world.platform.requests)
    # Everything held: a cycle asks nothing.
    await world.cycle("SE4")
    assert len(world.platform.requests) == requests


# ------------------------------------------------------------------ refusals and waits


async def test_a_rate_limit_is_waited_out_without_a_request(world: World) -> None:
    await world.start()
    world.platform.serve("SE4", TODAY, (429, "slow down", {"Retry-After": "120"}))

    first = await world.provider.async_fetch_day("SE4", TODAY)
    world.clock.now += timedelta(seconds=60)
    second = await world.provider.async_fetch_day("SE4", TODAY)

    assert first == DayFetch("failed", code="rate_limited", retry_after=timedelta(seconds=120))
    assert second == DayFetch("failed", code="rate_limited", retry_after=timedelta(seconds=60))
    assert len(world.platform.requests) == 1
    world.clock.now += timedelta(seconds=61)
    world.platform.serve_day("SE4", TODAY)
    assert (await world.provider.async_fetch_day("SE4", TODAY)).outcome == "fetched"
    assert len(world.platform.requests) == 2


async def test_a_server_error_waits_briefly(world: World) -> None:
    await world.start()
    world.platform.serve("SE4", TODAY, (503, "maintenance", {}))

    first = await world.provider.async_fetch_day("SE4", TODAY)
    again = await world.provider.async_fetch_day("SE4", TODAY)

    assert first.code == "entsoe_unavailable" and first.retry_after == timedelta(seconds=30)
    assert again.code == "entsoe_unavailable" and len(world.platform.requests) == 1


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        ((400, "bad", {}), "entsoe_rejected"),
        ((200, fixture("synthetic_ack_999_unknown.xml"), {}), "entsoe_rejected"),
        ((200, "<html>not a document</html>", {}), "provider_refused"),
        ((200, b"x" * (4 * 1024 * 1024 + 10), {}), "too_large"),
        (TimeoutError(), "timeout"),
        (ClientError("Cannot connect"), "network"),
    ],
)
async def test_failures_become_stable_codes(world: World, answer: Any, code: str) -> None:
    await world.start()
    world.platform.serve("SE4", TODAY, answer)

    fetch = await world.provider.async_fetch_day("SE4", TODAY)

    assert fetch.outcome == "failed" and fetch.code == code
    assert world.keys.entsoe_key_state == "ok"


async def test_a_day_still_being_published_is_not_published(world: World) -> None:
    await world.start()
    area = entsoe_area("SE4")
    start, _ = market_day_bounds(TODAY, area.market_tz)
    # Only the first two hours of the day: the platform is still writing it.
    body = entsoe_body(
        area.eic,
        start.strftime("%Y-%m-%dT%H:%MZ"),
        (start + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%MZ"),
        "PT15M",
        ["40.00"] * 8,
    )
    world.platform.serve("SE4", TODAY, (200, body, {}))

    assert await world.provider.async_fetch_day("SE4", TODAY) == DayFetch("not_published", code="not_published")


async def test_an_unchecked_key_is_proved_by_its_first_answer(world: World) -> None:
    await world.start(state="unchecked")
    world.platform.serve_day("SE4", TODAY)

    await world.provider.async_fetch_day("SE4", TODAY)

    assert world.keys.entsoe_key_state == "ok"


async def test_without_a_key_nothing_is_asked(world: World) -> None:
    await world.start(key=None)

    assert await world.provider.async_fetch_day("SE4", TODAY) == DayFetch("failed", code="source_missing")
    assert await world.provider.async_fetch_day("GB-A", TODAY) == DayFetch("failed", code="source_missing")
    assert world.platform.requests == []


# ------------------------------------------------------------------ a rejected key


@pytest.mark.parametrize(
    "answer",
    [(401, "Unauthorized", {}), (403, "", {}), (200, fixture("synthetic_ack_999_auth.xml"), {})],
)
async def test_a_rejected_key_leaves_its_areas_without_a_source_at_once(world: World, answer: Any) -> None:
    """A 401/403 during operation marks the key rejected; the router takes ENTSO-E off every area, which then has no
    source (nothing is asked for it) until the key is replaced, and says so."""
    serve(world.transport)
    await world.start()
    world.platform.serve("SE4", TODAY, answer)
    assert world.repository.provider_for("SE4").key == "entsoe"
    assert world.repository.provider_for("ES").key == "entsoe"

    await world.manager.async_subscribe(owner_id="charger", area_id="SE4")
    await world.hass.async_block_till_done()

    assert world.keys.entsoe_key_state == "rejected" and world.keys.entsoe_key_set
    assert world.changes == 1
    assert world.repository.provider_for("SE4") is None
    assert world.repository.provider_for("ES") is None
    # The cycle after the rejection asks nobody: no source is left.
    assert world.transport.calls == []
    snapshot = world.manager.area_snapshot("SE4")
    assert snapshot.today_state == "unavailable"
    assert snapshot.today_snapshot.attempt_error == "source_missing"
    assert world.repository.source_status("ES").notice.code == "entsoe_key_rejected"
    assert len(world.platform.requests) == 1


async def test_a_key_replaced_while_a_request_was_out_keeps_its_own_state(world: World) -> None:
    await world.start()

    def answer(_url: str) -> Any:
        world.hass.async_create_task(world.keys.async_set_entsoe_key(EntsoeToken("another-key"), "ok"))
        return (401, "", {})

    world.platform.serve("SE4", TODAY, answer)
    await world.provider.async_fetch_day("SE4", TODAY)
    await world.hass.async_block_till_done()

    assert world.keys.entsoe_key_state == "ok"


# ------------------------------------------------------------------ the choice of provider


@pytest.mark.parametrize(
    ("nordpool_serves", "nordpool_loaded", "entsoe_serves", "usable", "chosen"),
    [
        (True, True, True, True, "nordpool"),
        (True, True, True, False, "nordpool"),
        (True, False, True, True, "entsoe"),
        (True, False, True, False, None),
        (False, True, True, True, "entsoe"),
        (False, True, True, False, None),
        (False, False, False, True, None),
        (False, False, False, False, None),
    ],
)
def test_the_choice_rule(nordpool_serves: bool, nordpool_loaded: bool, entsoe_serves: bool, usable: bool, chosen: str) -> None:
    assert (
        choose_provider(
            nordpool_serves=nordpool_serves,
            nordpool_loaded=nordpool_loaded,
            entsoe_serves=entsoe_serves,
            entsoe_key_usable=usable,
        )
        == chosen
    )


async def test_the_router_follows_the_key_and_the_nordpool_entry(world: World) -> None:
    await world.start(key=None)
    assert world.repository.provider_for("ES") is None
    assert world.repository.area_entry("FI").market_tz == "Europe/Helsinki"

    await world.keys.async_set_entsoe_key(EntsoeToken(SECRET), "ok")
    assert world.changes == 1
    assert world.repository.provider_for("ES") is world.provider
    assert world.repository.provider_for("FI") is world.provider
    assert world.repository.area_entry("FI").market_tz == "Europe/Brussels"
    # No route on the platform: no source.
    assert world.repository.provider_for("GB-A") is None

    world.nordpool_entry.mock_state(world.hass, ConfigEntryState.LOADED)
    assert world.repository.provider_for("FI").key == "nordpool"
    assert world.repository.provider_for("ES") is world.provider

    world.nordpool_entry.mock_state(world.hass, ConfigEntryState.NOT_LOADED)
    await world.keys.async_set_entsoe_key(None)
    assert world.repository.provider_for("FI") is None
    assert world.changes == 4


async def test_setting_the_key_moves_a_subscribed_area_and_asks_at_once(world: World) -> None:
    """SE4's files kept from before 2.0 (the relay's) are dated on the same calendar as ENTSO-E's, so today's held
    file stays; inside the window tomorrow is asked of the platform at once."""
    await world.start(key=None)
    world.repository._adopt(  # noqa: SLF001 - the store's own path
        {"schema": 2, "days": {"SE4|Europe/Stockholm|2026-09-22": {
            "document": json.loads(relay_day_body("SE4", TODAY)), "fetched_at": "2026-09-21T12:00:00+00:00",
            "provider": "relay",
        }}}
    )
    world.clock.now = datetime(2026, 9, 22, 11, 0, tzinfo=UTC)
    world.platform.serve_day("SE4", TOMORROW)
    await world.manager.async_subscribe(owner_id="charger", area_id="SE4")
    await world.hass.async_block_till_done()
    assert world.platform.requests == []
    assert world.repository.day_snapshot("SE4", TODAY).document is not None

    await world.keys.async_set_entsoe_key(EntsoeToken(SECRET), "ok")
    await world.hass.async_block_till_done()

    assert world.platform.asked() == [utc_period("SE4", TOMORROW)]
    assert world.cache.payload["days"]["SE4|Europe/Stockholm|2026-09-23"]["provider"] == "entsoe"
    assert world.manager.area_snapshot("SE4").tomorrow_snapshot.is_complete


async def test_requests_go_one_at_a_time(world: World) -> None:
    await world.start()
    gate = asyncio.Event()
    inside = 0
    most = 0

    async def slow(day: date) -> None:
        nonlocal inside, most
        inside += 1
        most = max(most, inside)
        await gate.wait()
        inside -= 1

    original = world.platform.answer

    async def answer(url: str, headers: dict[str, str]) -> FakeResponse:
        await slow(TODAY)
        return await original(url, headers)

    world.platform.answer = answer  # type: ignore[method-assign]
    tasks = [world.hass.async_create_task(world.provider.async_fetch_day("SE4", day)) for day in (YESTERDAY, TODAY)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(*tasks)

    assert most == 1 and len(world.platform.requests) == 2


async def test_setup_builds_the_entsoe_provider_with_the_stored_key(hass: HomeAssistant, hass_storage) -> None:
    from custom_components.spotnav.pricing.price_repository import async_setup_price_repository
    from custom_components.spotnav.runtime import domain_data

    hass_storage["spotnav.price_sources"] = {
        "version": 1,
        "key": "spotnav.price_sources",
        "data": {"migrated": True, "entsoe_token": SECRET, "entsoe_token_state": "ok"},
    }
    repository = await async_setup_price_repository(hass, store=StoreDouble())
    try:
        assert domain_data(hass).price_store.entsoe_key_state == "ok"
        assert repository.provider_for("ES").key == "entsoe"
        assert repository.provider_for("SE4").key == "entsoe"
        await domain_data(hass).price_store.async_mark_entsoe_key(EntsoeToken(SECRET), "rejected")
        assert repository.provider_for("ES") is None
    finally:
        repository.stop_following_providers()
