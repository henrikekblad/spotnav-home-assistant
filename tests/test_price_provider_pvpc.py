"""Spain's PVPC through Home Assistant's own `pvpc_hourly_pricing` integration, from recorded archive days.

The integration's coordinator data is built from Red Eléctrica's archive files (`tests/fixtures/pvpc/`) the way
its library does it (`esios_api.parser.extract_prices_from_esios_public`): UTC instants from midnight of Home
Assistant's zone, one hour per row, EUR/kWh to five decimals. No network: the relay wire is the suite's stub.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav import time_zones
from custom_components.spotnav.pricing.area_catalogue import async_load_bundled
from custom_components.spotnav.pricing.price_refresh import PriceRefreshManager
from custom_components.spotnav.pricing.price_repository import PriceRepository
from custom_components.spotnav.pricing.providers import choose_provider, DayFetch, ProviderRouter
from custom_components.spotnav.pricing.providers.pvpc import PUBLICATION, PvpcProvider
from custom_components.spotnav.pricing.providers.relay import RelayProvider
from custom_components.spotnav.pricing.relay_contract import parse_day

from .relay import BASE_URL, Clock, FakeScheduler, StoreDouble, StubTransport

UTC = timezone.utc
MADRID = "Europe/Madrid"
FIXTURES = Path(__file__).parent / "fixtures" / "pvpc"
AREA = "ES-PVPC"

ORDINARY = date(2026, 10, 4)
AUTUMN = date(2025, 10, 26)
SPRING = date(2026, 3, 29)
FILES = {
    ORDINARY: "esios_PVPC_2026-10-04.json",
    AUTUMN: "esios_PVPC_2025-10-26_dst_autumn.json",
    SPRING: "esios_PVPC_2026-03-29_dst_spring.json",
}

#: Noon in Madrid on the ordinary day: before the 20:15 publication of the next.
NOW = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)


def archive(day: date) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / FILES[day]).read_text(encoding="utf-8"))["PVPC"]


def expected(day: date, column: str = "PCB") -> list[float]:
    """The relay's prices for the day: the archive's EUR/MWh with a decimal comma, per kWh, five decimals."""
    return [round(float(row[column].replace(",", ".")) / 1000.0, 5) for row in archive(day)]


def series(day: date, *, zone: str = MADRID, column: str = "PCB") -> dict[datetime, float]:
    """The coordinator's `sensors["PVPC"]` for one archive day, as `esios_api` builds it."""
    rows = archive(day)
    first = datetime.strptime(rows[0]["Dia"], "%d/%m/%Y").replace(tzinfo=ZoneInfo(zone)).astimezone(UTC)
    return {first + timedelta(hours=index): price for index, price in enumerate(expected(day, column))}


def coordinator(prices: Any) -> SimpleNamespace:
    """What `entry.runtime_data` holds: the coordinator, its `data` an `EsiosApiData`."""
    return SimpleNamespace(data=SimpleNamespace(sensors={"PVPC": prices}, availability={"PVPC": True}))


class World:
    def __init__(self, hass: HomeAssistant, clock: Clock, transport: StubTransport) -> None:
        self.hass = hass
        self.clock = clock
        self.transport = transport
        self.entry = pvpc_entry("2.0TD")
        self.scheduler = FakeScheduler()
        self.store = StoreDouble()

    def hold(self, prices: Any, entry: MockConfigEntry | None = None) -> None:
        (entry or self.entry).runtime_data = coordinator(prices)

    async def start(self, *, loaded: bool = True, zone: str = MADRID) -> None:
        await self.hass.config.async_set_time_zone(zone)
        await time_zones.async_resolve([zone])
        self.entry.add_to_hass(self.hass)
        if loaded:
            self.entry.mock_state(self.hass, ConfigEntryState.LOADED)
        bundled = await async_load_bundled(self.hass)
        assert bundled is not None
        self.provider = PvpcProvider(self.hass, bundled, now=self.clock)
        self.router = ProviderRouter(
            RelayProvider(self.hass, base_url=BASE_URL, session=self.transport), pvpc=self.provider
        )
        self.router.async_start(self.hass)
        self.repository = PriceRepository(
            self.hass, now=self.clock, store=self.store, bundled=bundled, router=self.router
        )
        self.manager = PriceRefreshManager(
            self.hass, self.repository, now=self.clock, jitter=lambda: 0.5, scheduler=self.scheduler
        )

    async def stop(self) -> None:
        await self.manager.async_shutdown()
        self.router.async_stop()
        for entry in self.hass.config_entries.async_entries("pvpc_hourly_pricing"):
            entry.mock_state(self.hass, ConfigEntryState.NOT_LOADED)


def pvpc_entry(tariff: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain="pvpc_hourly_pricing",
        title=f"PVPC - {tariff}",
        unique_id=tariff,
        data={"tariff": tariff, "power": 3.3, "power_p3": 3.3, "api_token": None},
    )


@pytest.fixture
async def world(hass: HomeAssistant, transport: StubTransport) -> World:
    instance = World(hass, Clock(NOW), transport)
    yield instance
    if hasattr(instance, "manager"):
        await instance.stop()


# ------------------------------------------------------------------ the document


async def test_an_archive_day_becomes_the_relays_day_document(world: World) -> None:
    await world.start()
    world.hold(series(ORDINARY))

    fetch = await world.provider.async_fetch_day(AREA, ORDINARY)

    assert fetch.outcome == "fetched"
    document = fetch.document
    assert document["v"] == 1 and document["area"] == AREA and document["date"] == "2026-10-04"
    assert document["tz"] == MADRID and "market_tz" not in document
    assert document["start"] == "2026-10-04T00:00:00+02:00"
    assert document["res"] == 60 and document["unit"] == "EUR/kWh"
    # The relay's own numbers: the archive's PCB per kWh, five decimals; no rate for a EUR area.
    assert document["prices"] == expected(ORDINARY)
    assert document["fx"] == {} and "fx_date" not in document
    assert document["src"] == "esios.ree.es"
    parsed = parse_day(document, area_id=AREA, day=ORDINARY)
    assert parsed.interval_count == 24 and parsed.covers_whole_day()


@pytest.mark.parametrize(("day", "hours"), [(AUTUMN, 25), (SPRING, 23)])
async def test_clock_change_days_keep_every_hour(world: World, day: date, hours: int) -> None:
    world.clock.now = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=10)
    await world.start()
    world.hold(series(day))

    fetch = await world.provider.async_fetch_day(AREA, day)

    assert fetch.outcome == "fetched"
    assert len(fetch.document["prices"]) == hours and fetch.document["prices"] == expected(day)
    parsed = parse_day(fetch.document, area_id=AREA, day=day)
    assert parsed.interval_count == hours and parsed.covers_whole_day()


@pytest.mark.parametrize("zone", ["Atlantic/Canary", "America/Los_Angeles"])
async def test_a_day_placed_in_another_home_zone_is_the_same_madrid_day(world: World, zone: str) -> None:
    """The integration starts a day at midnight of Home Assistant's own zone (the Canary Islands an hour after
    Madrid); the file is the archive's day from midnight in Madrid all the same."""
    await world.start(zone=zone)
    world.hold(series(ORDINARY, zone=zone))
    madrid = await world.provider.async_fetch_day(AREA, ORDINARY)
    assert madrid.outcome == "fetched"
    assert madrid.document["start"] == "2026-10-04T00:00:00+02:00"
    assert madrid.document["prices"] == expected(ORDINARY)


async def test_a_quarter_hour_series_is_read_at_fifteen_minutes(world: World) -> None:
    """With a personal ESIOS token the integration reads indicator 1001, whose step is read from the instants."""
    await world.start()
    start = datetime(2026, 10, 4, tzinfo=ZoneInfo(MADRID)).astimezone(UTC)
    world.hold({start + timedelta(minutes=15 * index): 0.1 + index / 10000 for index in range(96)})

    fetch = await world.provider.async_fetch_day(AREA, ORDINARY)

    assert fetch.outcome == "fetched" and fetch.document["res"] == 15 and len(fetch.document["prices"]) == 96
    assert parse_day(fetch.document, area_id=AREA, day=ORDINARY).covers_whole_day()


async def test_today_and_tomorrow_are_both_read_from_one_series(world: World) -> None:
    await world.start()
    following = ORDINARY + timedelta(days=1)
    start = datetime(2026, 10, 5, tzinfo=ZoneInfo(MADRID)).astimezone(UTC)
    world.hold({**series(ORDINARY), **{start + timedelta(hours=hour): 0.2 for hour in range(24)}})

    assert (await world.provider.async_fetch_day(AREA, ORDINARY)).document["prices"] == expected(ORDINARY)
    tomorrow = await world.provider.async_fetch_day(AREA, following)
    assert tomorrow.outcome == "fetched" and tomorrow.document["date"] == "2026-10-05"
    assert tomorrow.document["prices"] == [0.2] * 24


# ------------------------------------------------------------------ not there, refused


async def test_tomorrow_not_held_is_not_published_and_today_not_held_is_unavailable(world: World) -> None:
    await world.start()
    world.hold(series(ORDINARY))
    assert await world.provider.async_fetch_day(AREA, ORDINARY + timedelta(days=1)) == DayFetch(
        "not_published", code="not_published"
    )
    world.hold({})
    assert await world.provider.async_fetch_day(AREA, ORDINARY) == DayFetch("failed", code="pvpc_unavailable")


async def test_without_a_loaded_entry_or_its_data_the_pvpc_is_unavailable(world: World) -> None:
    await world.start(loaded=False)
    world.hold(series(ORDINARY))
    assert await world.provider.async_fetch_day(AREA, ORDINARY) == DayFetch("failed", code="pvpc_unavailable")

    world.entry.mock_state(world.hass, ConfigEntryState.LOADED)
    world.entry.runtime_data = None
    assert await world.provider.async_fetch_day(AREA, ORDINARY) == DayFetch("failed", code="pvpc_unavailable")
    world.entry.runtime_data = SimpleNamespace(data=None)
    assert await world.provider.async_fetch_day(AREA, ORDINARY) == DayFetch("failed", code="pvpc_unavailable")
    world.entry.runtime_data = SimpleNamespace(data=SimpleNamespace(sensors={"INJECTION": {}}))
    assert await world.provider.async_fetch_day(AREA, ORDINARY) == DayFetch("failed", code="pvpc_unavailable")


async def test_an_area_without_a_pvpc_route_is_not_served(world: World) -> None:
    await world.start()
    world.hold(series(ORDINARY))
    assert not world.provider.serves("ES") and world.provider.serves(AREA)
    assert await world.provider.async_fetch_day("ES", ORDINARY) == DayFetch("failed", code="source_missing")


def _drop_one(prices: dict[datetime, float]) -> dict[datetime, float]:
    return dict(list(prices.items())[:-1])


def _gap(prices: dict[datetime, float]) -> dict[datetime, float]:
    items = list(prices.items())
    return dict(items[:5] + items[6:])


def _set(index: int, value: Any):
    def damage(prices: dict[datetime, float]) -> dict[Any, Any]:
        items = list(prices.items())
        items[index] = (items[index][0], value)
        return dict(items)

    return damage


def _naive(prices: dict[datetime, float]) -> dict[Any, Any]:
    return {instant.replace(tzinfo=None): value for instant, value in prices.items()}


@pytest.mark.parametrize(
    "damage",
    [
        _drop_one,
        _gap,
        _set(3, None),
        _set(3, "0,123"),
        _set(3, math.nan),
        _set(3, True),
        _naive,
        lambda prices: list(prices.values()),
        lambda prices: {**prices, datetime(2026, 10, 4, 12, 30, tzinfo=UTC): 0.1},
    ],
)
async def test_a_series_this_client_cannot_place_is_refused(world: World, damage) -> None:
    await world.start()
    world.hold(damage(series(ORDINARY)))
    assert await world.provider.async_fetch_day(AREA, ORDINARY) == DayFetch("failed", code="provider_refused")


async def test_the_areas_tariff_is_preferred_and_another_is_the_persons_own(world: World) -> None:
    await world.start(loaded=False)
    ceuta = pvpc_entry("2.0TD (Ceuta/Melilla)")
    ceuta.add_to_hass(world.hass)
    ceuta.mock_state(world.hass, ConfigEntryState.LOADED)
    world.hold({instant: 9.0 for instant in series(ORDINARY)}, ceuta)
    # Only Ceuta and Melilla's entry: the person's own tariff is read.
    assert (await world.provider.async_fetch_day(AREA, ORDINARY)).document["prices"] == [9.0] * 24

    world.entry.mock_state(world.hass, ConfigEntryState.LOADED)
    world.hold(series(ORDINARY))
    assert (await world.provider.async_fetch_day(AREA, ORDINARY)).document["prices"] == expected(ORDINARY)


# ------------------------------------------------------------------ the choice and the schedule


@pytest.mark.parametrize(
    ("pvpc_serves", "pvpc_loaded", "octopus_serves", "chosen"),
    [
        (True, True, False, "pvpc"),
        (True, False, False, "relay"),
        (False, True, False, "relay"),
        (False, False, True, "octopus"),
        (False, False, False, "relay"),
    ],
)
def test_the_rule_for_the_pvpc_and_agile(pvpc_serves, pvpc_loaded, octopus_serves, chosen) -> None:
    """The PVPC and the GB regions have no Nord Pool code and no ENTSO-E route; without their integration they
    stay with the relay for now."""
    assert (
        choose_provider(
            nordpool_serves=False,
            nordpool_loaded=True,
            entsoe_serves=False,
            entsoe_key_usable=True,
            pvpc_serves=pvpc_serves,
            pvpc_loaded=pvpc_loaded,
            octopus_serves=octopus_serves,
        )
        == chosen
    )


async def test_the_router_follows_the_pvpc_entry(world: World) -> None:
    await world.start(loaded=False)
    world.hold(series(ORDINARY))
    heard: list[int] = []
    world.repository.add_provider_listener(lambda: heard.append(1))
    assert world.repository.provider_for(AREA).key == "relay"

    world.entry.mock_state(world.hass, ConfigEntryState.LOADED)
    assert world.repository.provider_for(AREA) is world.provider and heard == [1]
    entry = world.repository.area_entry(AREA)
    assert entry.market_tz == MADRID and entry.publication == PUBLICATION
    assert entry.included == ("grid_fee",)
    # Spain's spot price is another area: not the PVPC's.
    assert world.repository.provider_for("ES").key == "relay"

    world.entry.mock_state(world.hass, ConfigEntryState.NOT_LOADED)
    assert world.repository.provider_for(AREA).key == "relay" and heard == [1, 1]


async def test_a_subscribed_area_is_read_and_waits_for_the_evening_publication(world: World) -> None:
    await world.start()
    world.hold(series(ORDINARY))

    await world.manager.async_subscribe(owner_id="charger", area_id=AREA)
    await world.hass.async_block_till_done()

    snapshot = world.manager.area_snapshot(AREA)
    assert snapshot.today_state == "ready"
    assert world.repository.file_authority(AREA, ORDINARY + timedelta(days=1)) == "not_listed"
    held = world.repository.day_snapshot(AREA, ORDINARY).document
    assert list(held.prices) == expected(ORDINARY)

    # 20:30 in Madrid: the integration has read tomorrow, and the next cycle takes it.
    world.clock.now = datetime(2026, 10, 4, 18, 30, tzinfo=UTC)
    start = datetime(2026, 10, 5, tzinfo=ZoneInfo(MADRID)).astimezone(UTC)
    world.hold({**series(ORDINARY), **{start + timedelta(hours=hour): 0.2 for hour in range(24)}})
    await world.manager.async_refresh_area(AREA)
    await world.hass.async_block_till_done()
    assert world.manager.area_snapshot(AREA).tomorrow_state == "ready"


async def test_no_past_day_is_asked_for(world: World) -> None:
    """The integration holds today and tomorrow: a past day is out of reach and never read."""
    await world.start()
    world.hold(series(ORDINARY))
    assert await world.repository.async_get_archive_day(AREA, ORDINARY - timedelta(days=1)) is None


async def test_setup_builds_the_pvpc_provider(hass: HomeAssistant) -> None:
    from custom_components.spotnav.pricing.price_repository import async_setup_price_repository

    entry = pvpc_entry("2.0TD")
    entry.add_to_hass(hass)
    repository = await async_setup_price_repository(hass, store=StoreDouble())
    try:
        assert repository.provider_for(AREA).key == "relay"
        entry.mock_state(hass, ConfigEntryState.LOADED)
        assert repository.provider_for(AREA).key == "pvpc"
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
        assert repository.provider_for(AREA).key == "relay"
    finally:
        repository.stop_following_providers()
