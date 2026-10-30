"""Nord Pool through Home Assistant's own `nordpool` integration, with a fake `nordpool.get_prices_for_date`.

The fake service answers what the real one does (Home Assistant 2026.9, `components/nordpool/services.py`):
`{area: [{"start", "end", "price"}]}` for the CET delivery day in EUR/MWh, an empty list when nothing is out,
and `ServiceValidationError` with `connection_error` / `authentication_error`. No network: the relay wire is the
suite's stub and the exchange rates a fixed table.
"""

from __future__ import annotations

import asyncio
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.pricing.area_catalogue import async_load_bundled
from custom_components.spotnav.pricing.price_refresh import PriceRefreshManager
from custom_components.spotnav.pricing.price_repository import PriceRepository
from custom_components.spotnav.pricing.providers import (
    DayFetch,
    ProviderRouter,
    schedule_authority,
)
from custom_components.spotnav.pricing.providers import nordpool as nordpool_module
from custom_components.spotnav.pricing.providers.nordpool import NordPoolProvider, PUBLICATION
from custom_components.spotnav.pricing.providers.relay import RelayProvider
from custom_components.spotnav.pricing.relay_contract import parse_day

from .relay import BASE_URL, Clock, FakeScheduler, StoreDouble, StubTransport

UTC = timezone.utc
BRUSSELS = "Europe/Brussels"

#: A Friday morning before the publication: 11:00 in Brussels.
NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
TODAY = date(2026, 10, 30)
TOMORROW = date(2026, 10, 31)
YESTERDAY = date(2026, 10, 29)
SEK_RATE = 11.2163
RATE_DAY = date(2026, 10, 29)


def rows(day: date, *, zone: str = BRUSSELS, minutes: int = 15, price=None) -> list[dict[str, Any]]:
    """One delivery day as the service lists it: UTC instants, EUR/MWh."""
    tz = ZoneInfo(zone)
    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)
    following = day + timedelta(days=1)
    end = datetime(following.year, following.month, following.day, tzinfo=tz).astimezone(UTC)
    step = timedelta(minutes=minutes)
    out: list[dict[str, Any]] = []
    moment = start
    while moment < end:
        index = len(out)
        out.append(
            {
                "start": moment.isoformat(),
                "end": (moment + step).isoformat(),
                "price": (12.345 + index) if price is None else price(index),
            }
        )
        moment += step
    return out


class FakeNordPool:
    """`nordpool.get_prices_for_date`: answers per (area code, date); records every call's data."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers: dict[tuple[str, str], Any] = {}

    def serve(self, code: str, day: date, answer: Any) -> None:
        self.answers[(code, day.isoformat())] = answer

    async def handle(self, call: ServiceCall) -> dict[str, Any]:
        self.calls.append(dict(call.data))
        code = call.data["areas"][0]
        answer = self.answers.get((code, call.data["date"]), [])
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return await answer()
        if isinstance(answer, dict):
            return answer
        return {code: answer}

    def asked(self) -> list[tuple[str, str]]:
        return [(call["areas"][0], call["date"]) for call in self.calls]


class FakeRates:
    """The ECB rates: SEK held for the week, every other currency missing."""

    def __init__(self, rates: dict[str, float] | None = None) -> None:
        self.rates = {"SEK": SEK_RATE} if rates is None else rates
        self.started = 0
        self.refreshed = 0

    def rate_for(self, currency: str, day: date) -> tuple[float, date] | None:
        rate = self.rates.get(currency)
        return None if rate is None else (rate, RATE_DAY)

    async def async_start(self) -> None:
        self.started += 1

    async def async_refresh(self) -> bool:
        self.refreshed += 1
        return False


class World:
    def __init__(self, hass: HomeAssistant, clock: Clock, transport: StubTransport) -> None:
        self.hass = hass
        self.clock = clock
        self.transport = transport
        self.service = FakeNordPool()
        self.rates = FakeRates()
        self.entry = MockConfigEntry(domain="nordpool", title="Nord Pool", data={"areas": ["SE3"], "currency": "SEK"})
        self.scheduler = FakeScheduler()
        self.store = StoreDouble()

    async def start(self, *, loaded: bool = True, register: bool = True) -> None:
        if register:
            self.hass.services.async_register(
                "nordpool", "get_prices_for_date", self.service.handle, supports_response=SupportsResponse.ONLY
            )
        self.entry.add_to_hass(self.hass)
        if loaded:
            self.entry.mock_state(self.hass, ConfigEntryState.LOADED)
        bundled = await async_load_bundled(self.hass)
        assert bundled is not None
        self.provider = NordPoolProvider(self.hass, bundled, self.rates, now=self.clock)
        self.router = ProviderRouter(RelayProvider(self.hass, base_url=BASE_URL, session=self.transport), self.provider)
        self.router.async_start(self.hass)
        self.repository = PriceRepository(
            self.hass, now=self.clock, store=self.store, bundled=bundled, router=self.router
        )
        self.manager = PriceRefreshManager(
            self.hass, self.repository, now=self.clock, jitter=lambda: 0.5, scheduler=self.scheduler
        )

    async def cycle(self, area: str) -> Any:
        snapshot = await self.manager.async_refresh_area(area)
        await self.hass.async_block_till_done()
        return snapshot

    async def stop(self) -> None:
        await self.manager.async_shutdown()
        self.router.async_stop()
        # Home Assistant's own teardown would unload a loaded entry through the real integration.
        self.entry.mock_state(self.hass, ConfigEntryState.NOT_LOADED)


@pytest.fixture
async def world(hass: HomeAssistant, transport: StubTransport) -> World:
    instance = World(hass, Clock(NOW), transport)
    yield instance
    if hasattr(instance, "manager"):
        await instance.stop()


# ------------------------------------------------------------------ the document


async def test_a_quarter_hour_day_becomes_the_relays_day_document(world: World) -> None:
    await world.start()
    world.service.serve("SE3", TODAY, rows(TODAY, zone="Europe/Stockholm"))

    fetch = await world.provider.async_fetch_day("SE3", TODAY)

    assert fetch.outcome == "fetched"
    document = fetch.document
    assert world.service.calls == [
        {"config_entry": world.entry.entry_id, "date": "2026-10-30", "areas": ["SE3"], "currency": "EUR"}
    ]
    assert document["v"] == 1 and document["area"] == "SE3" and document["date"] == "2026-10-30"
    assert document["tz"] == "Europe/Stockholm" and "market_tz" not in document
    assert document["start"] == "2026-10-30T00:00:00+01:00"
    assert document["res"] == 15 and document["unit"] == "EUR/kWh"
    assert len(document["prices"]) == 96
    # EUR/MWh to EUR/kWh, five decimals as the relay publishes.
    assert document["prices"][0] == round(12.345 / 1000, 5) and document["prices"][1] == round(13.345 / 1000, 5)
    assert all(len(repr(price).split(".")[1]) <= 5 for price in document["prices"])
    assert document["fx"] == {"SEK": SEK_RATE}
    assert document["fx_date"] == "2026-10-29" and document["fx_src"] == "ECB"
    assert document["src"] == "nordpool"
    parsed = parse_day(document, area_id="SE3", day=TODAY)
    assert parsed.interval_count == 96 and parsed.covers_whole_day()
    assert parsed.fx_rate("SEK") == SEK_RATE and parsed.fx_date == RATE_DAY
    # The rates' daily timer starts with the first day that needs a rate.
    assert world.rates.started == 1


@pytest.mark.parametrize(
    ("area", "code", "zone", "day", "count"),
    [
        ("SE3", "SE3", "Europe/Stockholm", date(2026, 3, 29), 92),
        ("SE3", "SE3", "Europe/Stockholm", date(2026, 10, 25), 100),
        ("DE-LU", "GER", "Europe/Berlin", date(2026, 3, 29), 92),
        ("DE-LU", "GER", "Europe/Berlin", date(2026, 10, 25), 100),
    ],
)
async def test_clock_change_days_keep_every_quarter_hour(
    world: World, area: str, code: str, zone: str, day: date, count: int
) -> None:
    await world.start()
    world.service.serve(code, day, rows(day, zone=zone))

    fetch = await world.provider.async_fetch_day(area, day)

    assert len(fetch.document["prices"]) == count
    assert world.service.calls[0]["areas"] == [code]
    parsed = parse_day(fetch.document, area_id=area, day=day)
    assert parsed.interval_count == count and parsed.covers_whole_day()
    if area == "DE-LU":
        # A euro area needs no rate.
        assert fetch.document["fx"] == {} and "fx_date" not in fetch.document


async def test_an_hourly_day_is_read_at_sixty_minutes(world: World) -> None:
    await world.start()
    day = date(2025, 9, 15)
    world.service.serve("SE3", day, rows(day, zone="Europe/Stockholm", minutes=60))

    fetch = await world.provider.async_fetch_day("SE3", day)

    assert fetch.document["res"] == 60 and len(fetch.document["prices"]) == 24
    assert parse_day(fetch.document, area_id="SE3", day=day).resolution_minutes == 60


async def test_a_day_without_a_held_rate_asks_the_ecb_once_and_is_asked_again_when_the_rate_arrives(
    world: World,
) -> None:
    world.rates.rates = {}
    await world.start()
    world.service.serve("SE3", TODAY, rows(TODAY, zone="Europe/Stockholm"))

    snapshot = await world.repository.async_get_file("SE3", TODAY)

    assert snapshot.document is not None and snapshot.document.fx == {}
    assert world.rates.refreshed == 1
    # Held, no rate, and still none: nothing to ask.
    assert world.repository.worth_asking("SE3", TODAY) is False
    world.rates.rates = {"SEK": SEK_RATE}
    assert world.repository.worth_asking("SE3", TODAY) is True
    snapshot = await world.repository.async_get_file("SE3", TODAY)
    assert snapshot.document.fx_rate("SEK") == SEK_RATE
    assert world.service.asked() == [("SE3", "2026-10-30"), ("SE3", "2026-10-30")]
    assert world.repository.worth_asking("SE3", TODAY) is False


# ------------------------------------------------------------------ Finland on the CET calendar


async def test_a_helsinki_day_is_cut_from_two_cet_files(world: World) -> None:
    await world.start()
    for day in (YESTERDAY, TODAY):
        world.service.serve("FI", day, rows(day))

    snapshot = await world.repository.async_get_day("FI", TODAY)

    assert world.service.asked() == [("FI", "2026-10-29"), ("FI", "2026-10-30")]
    document = snapshot.document
    helsinki = ZoneInfo("Europe/Helsinki")
    assert document.start_instant == datetime(2026, 10, 30, tzinfo=helsinki).astimezone(UTC)
    assert document.interval_count == 96 and snapshot.is_complete
    # The last hour of the first file, then 23 hours of the second.
    assert [part.interval_count for part in document.parts] == [4, 92]
    assert document.intervals[0].eur_per_kwh == round((12.345 + 92) / 1000, 5)
    assert document.intervals[4].eur_per_kwh == round(12.345 / 1000, 5)
    # Stored under the CET calendar, beside any Helsinki file the relay left.
    assert set(world.store.payload["days"]) == {
        "FI|Europe/Brussels|2026-10-29",
        "FI|Europe/Brussels|2026-10-30",
    }
    assert world.store.payload["days"]["FI|Europe/Brussels|2026-10-30"]["provider"] == "nordpool"


async def test_the_helsinki_autumn_day_has_twenty_five_hours(world: World) -> None:
    await world.start()
    for day in (date(2026, 10, 24), date(2026, 10, 25)):
        world.service.serve("FI", day, rows(day))

    snapshot = await world.repository.async_get_day("FI", date(2026, 10, 25))

    assert snapshot.document.interval_count == 100 and snapshot.is_complete


async def test_finlands_tomorrow_is_published_only_with_its_principal_file(world: World) -> None:
    await world.start()
    for day in (YESTERDAY, TODAY):
        world.service.serve("FI", day, rows(day))
    await world.manager.async_subscribe(owner_id="charger", area_id="FI")
    await world.hass.async_block_till_done()

    snapshot = world.manager.area_snapshot("FI")
    # Before the window only today's two files were asked for; tomorrow's own is not asked.
    assert world.service.asked() == [("FI", "2026-10-29"), ("FI", "2026-10-30")]
    assert snapshot.state == "waiting_for_tomorrow" and snapshot.waiting_for_tomorrow
    assert snapshot.today_state == "ready"
    assert world.transport.calls == []

    world.clock.now = datetime(2026, 10, 30, 11, 45, tzinfo=UTC)  # 12:45 in Brussels
    snapshot = await world.cycle("FI")
    assert world.service.asked()[-1] == ("FI", "2026-10-31")
    assert snapshot.waiting_for_tomorrow and snapshot.tomorrow_authority == "not_listed"

    world.service.serve("FI", TOMORROW, rows(TOMORROW))
    world.clock.now += timedelta(minutes=5)
    snapshot = await world.cycle("FI")
    assert snapshot.state == "ready" and not snapshot.waiting_for_tomorrow
    assert snapshot.tomorrow_snapshot.is_complete
    assert snapshot.catalogue.market_tz == BRUSSELS and snapshot.catalogue.publication == PUBLICATION


# ------------------------------------------------------------------ not published, schedule, calls


async def test_an_empty_answer_is_not_published_and_caches_nothing(world: World) -> None:
    await world.start()
    world.service.serve("SE3", TODAY, rows(TODAY, zone="Europe/Stockholm"))
    world.clock.now = datetime(2026, 10, 30, 11, 50, tzinfo=UTC)  # inside the window
    await world.manager.async_subscribe(owner_id="charger", area_id="SE3")
    await world.hass.async_block_till_done()

    assert await world.provider.async_fetch_day("SE3", TOMORROW) == DayFetch("not_published", code="not_published")
    snapshot = world.manager.area_snapshot("SE3")
    assert snapshot.tomorrow_authority == "not_listed" and snapshot.state == "waiting_for_tomorrow"
    assert snapshot.tomorrow_snapshot.document is None and snapshot.tomorrow_snapshot.attempt_error is None
    assert "SE3|Europe/Stockholm|2026-10-31" not in world.store.payload["days"]
    # Not a failure: the publication cadence, not a backoff.
    assert snapshot.next_attempt == world.clock.now + timedelta(minutes=5)


async def test_one_call_per_file_per_attempt_and_none_when_everything_is_held(world: World) -> None:
    await world.start()
    world.service.serve("SE3", TODAY, rows(TODAY, zone="Europe/Stockholm"))
    world.service.serve("SE3", TOMORROW, rows(TOMORROW, zone="Europe/Stockholm"))

    await world.manager.async_subscribe(owner_id="charger", area_id="SE3")
    await world.hass.async_block_till_done()
    # Before the window: today only.
    assert world.service.asked() == [("SE3", "2026-10-30")]
    await world.cycle("SE3")
    assert world.service.asked() == [("SE3", "2026-10-30")]

    world.clock.now = datetime(2026, 10, 30, 11, 41, tzinfo=UTC)  # 12:41 Brussels: the window is open
    snapshot = await world.cycle("SE3")
    assert world.service.asked() == [("SE3", "2026-10-30"), ("SE3", "2026-10-31")]
    assert snapshot.state == "ready"

    world.clock.now += timedelta(hours=1)
    await world.cycle("SE3")
    assert len(world.service.calls) == 2
    assert world.transport.calls == []
    assert all(set(call) == {"config_entry", "date", "areas", "currency"} for call in world.service.calls)


def test_the_schedule_authority() -> None:
    def authority(day: date, now: datetime, **kwargs: Any) -> str:
        arguments = {
            "held": False,
            "attempt_at": None,
            "attempt_failed": False,
            "not_published_at": None,
        } | kwargs
        return schedule_authority(day, now=now, market_tz="Europe/Stockholm", publication=PUBLICATION, **arguments)

    morning = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
    window = datetime(2026, 10, 30, 11, 41, tzinfo=UTC)
    assert authority(TOMORROW, morning, held=True) == "listed"
    assert authority(TODAY, morning) == "unknown"
    assert authority(TOMORROW, morning) == "not_listed"
    assert authority(date(2026, 11, 1), window) == "not_listed"
    assert authority(TOMORROW, window) == "unknown"
    assert authority(TOMORROW, window, attempt_at=window, not_published_at=window) == "not_listed"
    assert authority(TOMORROW, window, attempt_at=window, attempt_failed=True) == "unknown"
    later = window + timedelta(minutes=5)
    assert authority(TOMORROW, later, attempt_at=later, not_published_at=window) == "unknown"


async def test_an_archive_day_beyond_nord_pools_reach_is_not_asked_for(world: World) -> None:
    await world.start()
    old = TODAY - timedelta(days=56)
    recent = TODAY - timedelta(days=55)
    world.service.serve("SE3", recent, rows(recent, zone="Europe/Stockholm"))

    assert await world.repository.async_get_archive_day("SE3", old) is None
    assert world.service.calls == []
    assert (await world.repository.async_get_archive_day("SE3", recent)) is not None
    assert world.service.asked() == [("SE3", recent.isoformat())]


# ------------------------------------------------------------------ refusals


def _mixed(day: date) -> list[dict[str, Any]]:
    out = rows(day, zone="Europe/Stockholm")
    first = datetime.fromisoformat(out[0]["start"])
    out[0] = {"start": first.isoformat(), "end": (first + timedelta(minutes=30)).isoformat(), "price": 1.0}
    return out


def _gap(day: date) -> list[dict[str, Any]]:
    out = rows(day, zone="Europe/Stockholm")
    del out[10]
    return out


def _duplicate(day: date) -> list[dict[str, Any]]:
    out = rows(day, zone="Europe/Stockholm")
    out.insert(5, dict(out[5]))
    return out


def _shifted(day: date) -> list[dict[str, Any]]:
    out = []
    for row in rows(day, zone="Europe/Stockholm"):
        start = datetime.fromisoformat(row["start"]) + timedelta(hours=1)
        end = datetime.fromisoformat(row["end"]) + timedelta(hours=1)
        out.append({"start": start.isoformat(), "end": end.isoformat(), "price": row["price"]})
    return out


def _too_many(day: date) -> list[dict[str, Any]]:
    out = rows(day, zone="Europe/Stockholm")
    last = datetime.fromisoformat(out[-1]["end"])
    for index in range(4):
        start = last + timedelta(minutes=15 * index)
        out.append({"start": start.isoformat(), "end": (start + timedelta(minutes=15)).isoformat(), "price": 1.0})
    return out


def _price(value: Any):
    def build(day: date) -> list[dict[str, Any]]:
        out = rows(day, zone="Europe/Stockholm")
        out[3] = {**out[3], "price": value}
        return out

    return build


@pytest.mark.parametrize(
    ("build", "code"),
    [
        (_mixed, "provider_refused"),
        (_gap, "provider_refused"),
        (_duplicate, "provider_refused"),
        (_shifted, "invalid_coverage"),
        (_too_many, "invalid_coverage"),
        (_price(None), "provider_refused"),
        (_price("12.5"), "provider_refused"),
        (_price(math.nan), "provider_refused"),
        (_price(True), "provider_refused"),
    ],
    ids=["mixed", "gap", "duplicate", "not_at_midnight", "too_many", "null", "text", "nan", "bool"],
)
async def test_a_day_this_client_cannot_read_is_refused(world: World, build, code: str) -> None:
    await world.start()
    world.service.serve("SE3", TODAY, build(TODAY))

    snapshot = await world.repository.async_get_file("SE3", TODAY)

    assert snapshot.document is None and snapshot.state == "invalid"
    assert snapshot.attempt_error == code


async def test_an_answer_for_another_area_is_refused(world: World) -> None:
    await world.start()
    world.service.serve("SE3", TODAY, {"SE4": rows(TODAY, zone="Europe/Stockholm")})

    assert await world.provider.async_fetch_day("SE3", TODAY) == DayFetch("failed", code="provider_refused")


# ------------------------------------------------------------------ errors


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ServiceValidationError(translation_domain="nordpool", translation_key="connection_error"), "nordpool_error"),
        (
            ServiceValidationError(translation_domain="nordpool", translation_key="authentication_error"),
            "nordpool_rejected",
        ),
        (
            ServiceValidationError(translation_domain="homeassistant", translation_key="service_config_entry_not_loaded"),
            "nordpool_unavailable",
        ),
        (vol.Invalid("Area XX is not valid"), "nordpool_rejected"),
    ],
    ids=["connection", "authentication", "entry_not_loaded", "area_refused"],
)
async def test_service_errors_become_stable_codes(world: World, error: Exception, code: str) -> None:
    await world.start()
    world.service.serve("SE3", TODAY, error)

    assert await world.provider.async_fetch_day("SE3", TODAY) == DayFetch("failed", code=code)


async def test_without_the_service_nord_pool_is_unavailable(world: World) -> None:
    await world.start(register=False)

    assert await world.provider.async_fetch_day("SE3", TODAY) == DayFetch("failed", code="nordpool_unavailable")


async def test_a_call_that_does_not_answer_in_time_is_a_nordpool_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nordpool_module, "REQUEST_TIMEOUT_S", 0.01)
    await world.start()

    async def never() -> Any:
        await asyncio.Event().wait()

    world.service.serve("SE3", TODAY, never)

    assert await world.provider.async_fetch_day("SE3", TODAY) == DayFetch("failed", code="nordpool_error")


async def test_a_connection_error_backs_off_and_keeps_the_area_unavailable(world: World) -> None:
    await world.start()
    world.service.serve(
        "SE3", TODAY, ServiceValidationError(translation_domain="nordpool", translation_key="connection_error")
    )

    await world.manager.async_subscribe(owner_id="charger", area_id="SE3")
    await world.hass.async_block_till_done()

    snapshot = world.manager.area_snapshot("SE3")
    assert snapshot.state == "unavailable" and snapshot.today_snapshot.attempt_error == "nordpool_error"
    # The first backoff step (60 s), not the healthy half hour.
    assert snapshot.next_attempt == world.clock.now + timedelta(seconds=60)


# ------------------------------------------------------------------ the choice of provider


async def test_the_router_follows_the_nordpool_entry(world: World) -> None:
    await world.start(loaded=False)
    world.service.serve("FI", YESTERDAY, rows(YESTERDAY))
    world.service.serve("FI", TODAY, rows(TODAY))
    assert world.repository.provider_for("FI").key == "relay"
    assert world.repository.provider_for("GB-A").key == "relay"

    world.entry.mock_state(world.hass, ConfigEntryState.LOADED)
    assert world.repository.provider_for("FI") is world.provider
    assert world.repository.area_entry("FI").market_tz == BRUSSELS
    # An area Nord Pool does not trade stays with the relay.
    assert world.repository.provider_for("ES").key == "relay"

    world.entry.mock_state(world.hass, ConfigEntryState.NOT_LOADED)
    assert world.repository.provider_for("FI").key == "relay"
    assert world.repository.area_entry("FI").market_tz == "Europe/Helsinki"


async def test_loading_the_nordpool_entry_moves_a_subscribed_area_and_asks_at_once(
    world: World, transport: StubTransport
) -> None:
    await world.start(loaded=False)
    transport.serve("/v2/areas.json", 503, "down")
    transport.serve("/v1/areas.json", 503, "down")
    transport.serve("/v2/index.json", 503, "down")
    transport.serve("/v1/index.json", 503, "down")
    world.service.serve("FI", YESTERDAY, rows(YESTERDAY))
    world.service.serve("FI", TODAY, rows(TODAY))
    await world.manager.async_subscribe(owner_id="charger", area_id="FI")
    await world.hass.async_block_till_done()
    assert world.service.calls == []

    world.entry.mock_state(world.hass, ConfigEntryState.LOADED)
    await world.hass.async_block_till_done()

    snapshot = world.manager.area_snapshot("FI")
    assert world.service.asked() == [("FI", "2026-10-29"), ("FI", "2026-10-30")]
    assert snapshot.today_state == "ready" and snapshot.catalogue.market_tz == BRUSSELS


async def test_without_a_loaded_entry_nothing_is_called(world: World) -> None:
    await world.start(loaded=False)

    assert await world.provider.async_fetch_day("SE3", TODAY) == DayFetch("failed", code="nordpool_unavailable")
    assert world.service.calls == []


async def test_setup_builds_the_router_and_the_rates_for_the_installation(hass: HomeAssistant) -> None:
    from custom_components.spotnav.pricing.price_repository import async_setup_price_repository
    from custom_components.spotnav.runtime import domain_data

    entry = MockConfigEntry(domain="nordpool", title="Nord Pool", data={})
    entry.add_to_hass(hass)
    repository = await async_setup_price_repository(hass, store=StoreDouble())
    try:
        assert domain_data(hass).fx_rates is not None
        assert repository.provider_for("SE3").key == "relay"
        entry.mock_state(hass, ConfigEntryState.LOADED)
        assert repository.provider_for("SE3").key == "nordpool"
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
        assert repository.provider_for("SE3").key == "relay"
    finally:
        repository.stop_following_providers()
