"""Zonneplan's dynamic tariff through the Zonneplan integration (HACS), from recorded entity states.

The integration's tariff sensor is registered and given the state it writes (`sensor.py`, `const.py`,
`coordinators/electricity_prices_data_coordinator.py` of version 2026.10.0): a `forecast` attribute listing
`{"start_date", "end_date", "price_tax_included": {"amount"}, "price_tax_excluded": {"amount"},
"sustainability_score": {"permille"}}` with aware datetimes in Amsterdam and amounts in 1e-7 EUR per kWh. The
forecast may start at today's midnight or at the current quarter-hour; both are built here. No network.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav import time_zones
from custom_components.spotnav.pricing.area_catalogue import async_load_bundled
from custom_components.spotnav.pricing.price_refresh import PriceRefreshManager
from custom_components.spotnav.pricing.price_repository import PriceRepository
from custom_components.spotnav.pricing.providers import choose_provider, DayFetch, ProviderRouter
from custom_components.spotnav.pricing.providers.nordpool import NordPoolProvider
from custom_components.spotnav.pricing.providers.pvpc import PvpcProvider
from custom_components.spotnav.pricing.providers.zonneplan import PUBLICATION, ZonneplanProvider
from custom_components.spotnav.pricing.relay_contract import parse_day

from .relay import Clock, FakeScheduler, StoreDouble, StubTransport

UTC = timezone.utc
AMSTERDAM = "Europe/Amsterdam"
AREA = "NL-ZONNEPLAN"
QUARTER = timedelta(minutes=15)
HOUR = timedelta(hours=1)
CONNECTION = "9a3c0f6e-1d2b-4c5d-8e7f-001122334455"

#: 12:00 in Amsterdam on 2026-10-04: today's tariff is out, tomorrow's not yet.
NOW = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
TODAY = date(2026, 10, 4)
TOMORROW = date(2026, 10, 5)


def midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=ZoneInfo(AMSTERDAM)).astimezone(UTC)


def amount(instant: datetime) -> int:
    """A made-up tariff in 1e-7 EUR/kWh: between 0.15 and 0.2460 EUR, a new value every quarter-hour."""
    return 1_500_000 + int(instant.timestamp() // 900) % 97 * 10_000


def instants(day: date, step: timedelta = QUARTER) -> list[datetime]:
    start, end = midnight(day), midnight(day + timedelta(days=1))
    found = []
    while start < end:
        found.append(start)
        start += step
    return found


def forecast(
    slots: list[datetime],
    *,
    step: timedelta = QUARTER,
    as_text: bool = False,
    price: Callable[[datetime], int] = amount,
) -> list[dict[str, Any]]:
    """The coordinator's `price_per_quarter_hour`: Amsterdam datetimes, amounts in 1e-7 EUR/kWh."""
    zone = ZoneInfo(AMSTERDAM)
    rows = []
    for start in slots:
        row: dict[str, Any] = {
            "start_date": start.astimezone(zone),
            "end_date": (start + step).astimezone(zone),
            "price_tax_included": {"amount": price(start)},
            "price_tax_excluded": {"amount": int(price(start) / 1.21) - 916_100},
            "sustainability_score": {"permille": 500},
        }
        if as_text:
            row = {**row, "start_date": row["start_date"].isoformat(), "end_date": row["end_date"].isoformat()}
        rows.append(row)
    return rows


def expected(slots: list[datetime]) -> list[float]:
    return [round(amount(slot) * 1e-7, 5) for slot in slots]


class Connection:
    """One electricity connection's tariff sensor, registered as the integration registers it."""

    def __init__(self, hass: HomeAssistant, entry: MockConfigEntry, uuid: str = CONNECTION) -> None:
        self.hass = hass
        registered = er.async_get(hass).async_get_or_create(
            "sensor",
            "zonneplan_one",
            f"{uuid}_current_quarter_hourly_electricity_tariff",
            config_entry=entry,
            suggested_object_id=f"renamed_tariff_{uuid[:4]}",
            translation_key="current_quarter_hourly_electricity_tariff",
        )
        self.entity_id = registered.entity_id

    def publish(self, rows: list[dict[str, Any]] | None, now: datetime) -> None:
        attributes: dict[str, Any] = {"unit_of_measurement": "€/kWh"}
        if rows is not None:
            attributes["forecast"] = rows
        self.hass.states.async_set(self.entity_id, "0.25", attributes)


class World:
    def __init__(self, hass: HomeAssistant, clock: Clock, transport: StubTransport) -> None:
        self.hass = hass
        self.clock = clock
        self.transport = transport
        self.entry = MockConfigEntry(domain="zonneplan_one", title="Zonneplan", data={})
        self.scheduler = FakeScheduler()
        self.store = StoreDouble()
        self.heard: list[int] = []

    async def start(self, *, loaded: bool = True, sensor: bool = True) -> None:
        await self.hass.config.async_set_time_zone(AMSTERDAM)
        await time_zones.async_resolve([AMSTERDAM, "Europe/Brussels"])
        self.entry.add_to_hass(self.hass)
        if sensor:
            self.connection = Connection(self.hass, self.entry)
        if loaded:
            self.entry.mock_state(self.hass, ConfigEntryState.LOADED)
        bundled = await async_load_bundled(self.hass)
        assert bundled is not None
        self.provider = ZonneplanProvider(self.hass, bundled, now=self.clock)
        # Nord Pool and the PVPC beside it, as in production: their areas' notices must not change.
        self.router = ProviderRouter(
            NordPoolProvider(self.hass, bundled, None, now=self.clock),
            pvpc=PvpcProvider(self.hass, bundled, now=self.clock),
            zonneplan=self.provider,
        )
        self.router.async_start(self.hass)
        self.router.add_listener(lambda: self.heard.append(1))
        self.repository = PriceRepository(
            self.hass, now=self.clock, store=self.store, bundled=bundled, router=self.router
        )
        self.manager = PriceRefreshManager(
            self.hass, self.repository, now=self.clock, jitter=lambda: 0.5, scheduler=self.scheduler
        )

    async def publish(self, slots: list[datetime], **kwargs: Any) -> None:
        self.connection.publish(forecast(slots, **kwargs), self.clock.now)
        await self.hass.async_block_till_done()

    async def fetch(self, day: date, area_id: str = AREA) -> DayFetch:
        return await self.provider.async_fetch_day(area_id, day)

    def notice(self, area_id: str) -> tuple[str | None, str | None]:
        status = self.repository.source_status(area_id)
        return status.key, None if status.notice is None else status.notice.code

    async def stop(self) -> None:
        await self.manager.async_shutdown()
        self.router.async_stop()
        self.entry.mock_state(self.hass, ConfigEntryState.NOT_LOADED)


@pytest.fixture
async def world(hass: HomeAssistant, transport: StubTransport) -> World:
    instance = World(hass, Clock(NOW), transport)
    yield instance
    if hasattr(instance, "manager"):
        await instance.stop()


# ------------------------------------------------------------------ the document


async def test_today_from_midnight_is_an_all_in_amsterdam_day(world: World) -> None:
    await world.start()
    await world.publish(instants(TODAY))

    fetch = await world.fetch(TODAY)

    assert fetch.outcome == "fetched"
    document = fetch.document
    assert document["v"] == 1 and document["area"] == AREA and document["date"] == "2026-10-04"
    assert document["tz"] == AMSTERDAM and "market_tz" not in document
    assert document["start"] == "2026-10-04T00:00:00+02:00"
    assert document["res"] == 15 and document["unit"] == "EUR/kWh"
    assert document["prices"] == expected(instants(TODAY))
    assert document["fx"] == {} and "fx_date" not in document and document["src"] == "zonneplan.nl"
    parsed = parse_day(document, area_id=AREA, day=TODAY)
    assert parsed.interval_count == 96 and parsed.covers_whole_day()


async def test_the_amount_is_in_ten_millionths_of_a_euro_including_tax(world: World) -> None:
    await world.start()
    world.connection.publish(forecast(instants(TODAY), price=lambda _instant: 2_500_000), world.clock.now)

    fetch = await world.fetch(TODAY)

    assert fetch.outcome == "fetched" and set(fetch.document["prices"]) == {0.25}


async def test_tomorrow_is_not_published_before_zonneplan_and_whole_after(world: World) -> None:
    await world.start()
    await world.publish(instants(TODAY))
    assert await world.fetch(TOMORROW) == DayFetch("not_published", code="not_published")

    # The afternoon: the forecast reaches tomorrow, but only in part at first.
    world.clock.now = datetime(2026, 10, 4, 12, 30, tzinfo=UTC)
    await world.publish(instants(TODAY) + instants(TOMORROW)[:40])
    assert await world.fetch(TOMORROW) == DayFetch("not_published", code="not_published")

    await world.publish(instants(TODAY) + instants(TOMORROW))
    fetch = await world.fetch(TOMORROW)
    assert fetch.outcome == "fetched" and fetch.document["prices"] == expected(instants(TOMORROW))


async def test_a_forecast_starting_now_holds_no_today_and_invents_no_price(world: World) -> None:
    """The first fetch after Zonneplan was added may start at the current quarter-hour. A day file needs a price for
    every slot from midnight, and none is made up: today is not held, tomorrow is, and nothing is stored for today."""
    await world.start()
    # 14:37 in Amsterdam, tomorrow is out; the forecast starts at the quarter-hour in progress.
    world.clock.now = datetime(2026, 10, 4, 12, 37, tzinfo=UTC)
    known = [slot for slot in instants(TODAY) if slot >= datetime(2026, 10, 4, 12, 30, tzinfo=UTC)]
    await world.publish(known + instants(TOMORROW))

    assert await world.fetch(TODAY) == DayFetch("failed", code="zonneplan_partial_day")
    assert (await world.fetch(TOMORROW)).document["prices"] == expected(instants(TOMORROW))

    await world.manager.async_subscribe(owner_id="charger", area_id=AREA)
    await world.hass.async_block_till_done()
    snapshot = world.manager.area_snapshot(AREA)
    # Today reads as not there (a source that is there but cannot give the day), never as invalid.
    assert snapshot.today_state == "unavailable" and snapshot.tomorrow_state == "ready"
    assert not world.repository.holds_file(AREA, TODAY) and world.repository.holds_file(AREA, TOMORROW)
    assert all(key.split("|")[2] != TODAY.isoformat() for key in world.repository._payload()["days"])


async def test_a_forecast_starting_later_than_now_is_not_today(world: World) -> None:
    await world.start()
    await world.publish([slot for slot in instants(TODAY) if slot >= NOW + HOUR])
    assert await world.fetch(TODAY) == DayFetch("failed", code="zonneplan_unavailable")


async def test_tomorrow_without_its_first_slots_is_refused(world: World) -> None:
    await world.start()
    world.clock.now = datetime(2026, 10, 4, 12, 30, tzinfo=UTC)
    await world.publish(instants(TODAY) + instants(TOMORROW)[4:])
    assert await world.fetch(TOMORROW) == DayFetch("failed", code="provider_refused")


@pytest.mark.parametrize(
    ("day", "slots"),
    [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100)],
)
async def test_clock_change_days_keep_every_quarter_hour(world: World, day: date, slots: int) -> None:
    world.clock.now = midnight(day) + timedelta(hours=10)
    await world.start()
    await world.publish(instants(day))

    fetch = await world.fetch(day)

    assert fetch.outcome == "fetched" and len(fetch.document["prices"]) == slots
    assert fetch.document["prices"] == expected(instants(day))
    parsed = parse_day(fetch.document, area_id=AREA, day=day)
    assert parsed.interval_count == slots and parsed.covers_whole_day()


async def test_an_hourly_forecast_is_an_hourly_day(world: World) -> None:
    await world.start()
    await world.publish(instants(TODAY, HOUR), step=HOUR)
    fetch = await world.fetch(TODAY)
    assert fetch.outcome == "fetched" and fetch.document["res"] == 60 and len(fetch.document["prices"]) == 24


async def test_text_instants_are_read_the_same(world: World) -> None:
    await world.start()
    await world.publish(instants(TODAY), as_text=True)
    assert (await world.fetch(TODAY)).document["prices"] == expected(instants(TODAY))


# ------------------------------------------------------------------ not there, refused


async def test_before_the_first_fetch_there_is_nothing_to_read(world: World) -> None:
    await world.start()
    world.connection.publish(None, world.clock.now)
    assert await world.fetch(TODAY) == DayFetch("failed", code="zonneplan_unavailable")


async def test_today_that_stops_early_is_unavailable_and_a_gap_refused(world: World) -> None:
    await world.start()
    await world.publish(instants(TODAY)[:80])
    assert await world.fetch(TODAY) == DayFetch("failed", code="zonneplan_unavailable")
    slots = instants(TODAY)
    await world.publish(slots[:50] + slots[51:])
    assert await world.fetch(TODAY) == DayFetch("failed", code="provider_refused")


async def test_a_past_day_is_out_of_reach(world: World) -> None:
    await world.start()
    yesterday = TODAY - timedelta(days=1)
    await world.publish(instants(yesterday) + instants(TODAY))
    # The forecast's past slots are never a past day: the integration keeps none whole after the fact.
    assert not world.repository.archive_reachable(AREA, yesterday)
    assert await world.repository.async_get_archive_day(AREA, yesterday) is None


def _damage(change: Callable[[dict[str, Any]], Any]):
    def damage(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows[3] = dict(rows[3])
        change(rows[3])
        return rows

    return damage


@pytest.mark.parametrize(
    "damage",
    [
        _damage(lambda row: row.__setitem__("price_tax_included", {"amount": "2500000"})),
        _damage(lambda row: row.__setitem__("price_tax_included", {"amount": True})),
        _damage(lambda row: row.__setitem__("price_tax_included", {"amount": float("nan")})),
        _damage(lambda row: row.__setitem__("price_tax_included", {"amount": 10**10})),
        _damage(lambda row: row.__setitem__("price_tax_included", 2_500_000)),
        _damage(lambda row: row.pop("price_tax_included")),
        _damage(lambda row: row.__setitem__("start_date", row["start_date"].replace(tzinfo=None))),
        _damage(lambda row: row.__setitem__("end_date", row["start_date"] + timedelta(minutes=20))),
        _damage(lambda row: row.__setitem__("start_date", "not a time")),
        lambda rows: rows + ["a row"],
        lambda rows: {"prices": rows},
    ],
)
async def test_a_forecast_this_client_cannot_read_is_refused(world: World, damage) -> None:
    await world.start()
    world.connection.publish(damage(forecast(instants(TODAY))), world.clock.now)
    assert await world.fetch(TODAY) == DayFetch("failed", code="provider_refused")


async def test_two_connections_tell_the_same_tariff_and_disagreeing_ones_are_refused(world: World) -> None:
    await world.start()
    second = Connection(world.hass, world.entry, "5e6f7a8b-0000-4000-8000-0000000000aa")
    await world.hass.async_block_till_done()
    # Found through the registry's own event.
    assert len(world.provider.entities) == 2
    await world.publish(instants(TODAY)[:48])
    second.publish(forecast(instants(TODAY)), world.clock.now)
    # Pooled: the first sensor's half day and the second's whole day agree.
    assert (await world.fetch(TODAY)).document["prices"] == expected(instants(TODAY))

    second.publish(forecast(instants(TODAY), price=lambda instant: amount(instant) + 1), world.clock.now)
    assert await world.fetch(TODAY) == DayFetch("failed", code="provider_refused")


# ------------------------------------------------------------------ the sensor, the choice and the schedule


async def test_the_sensor_is_found_by_unique_id_whatever_its_entity_id(world: World) -> None:
    await world.start()
    assert world.connection.entity_id == "sensor.renamed_tariff_9a3c"
    assert world.provider.entities == ("sensor.renamed_tariff_9a3c",)
    assert world.provider.serves(AREA) and world.repository.provider_for(AREA) is world.provider


async def test_another_zonneplan_sensor_is_not_the_tariff(world: World) -> None:
    await world.start(sensor=False)
    er.async_get(world.hass).async_get_or_create(
        "sensor", "zonneplan_one", f"{CONNECTION}_current_hourly_electricity_tariff", config_entry=world.entry
    )
    er.async_get(world.hass).async_get_or_create(
        "sensor", "other_integration", f"{CONNECTION}_current_quarter_hourly_electricity_tariff"
    )
    await world.hass.async_block_till_done()
    assert world.provider.entities == () and world.repository.provider_for(AREA) is None


async def test_the_spot_area_is_never_served_by_zonneplan(world: World) -> None:
    await world.start()
    await world.publish(instants(TODAY))
    assert not world.provider.serves("NL") and world.repository.provider_for("NL") is None
    assert await world.fetch(TODAY, "NL") == DayFetch("failed", code="source_missing")


@pytest.mark.parametrize(
    ("zonneplan_serves", "nordpool_serves", "chosen"),
    [(True, False, "zonneplan"), (False, True, "nordpool"), (False, False, None)],
)
def test_the_rule_for_zonneplan(zonneplan_serves: bool, nordpool_serves: bool, chosen: str | None) -> None:
    assert (
        choose_provider(
            nordpool_serves=nordpool_serves,
            nordpool_loaded=True,
            entsoe_serves=False,
            entsoe_key_usable=False,
            zonneplan_serves=zonneplan_serves,
        )
        == chosen
    )


async def test_the_router_follows_the_entry_and_the_sensor(world: World) -> None:
    await world.start(loaded=False)
    assert world.repository.provider_for(AREA) is None

    world.entry.mock_state(world.hass, ConfigEntryState.LOADED)
    assert world.repository.provider_for(AREA) is world.provider and world.heard == [1]
    entry = world.repository.area_entry(AREA)
    assert entry.market_tz == AMSTERDAM and entry.tz == AMSTERDAM and entry.publication == PUBLICATION
    assert entry.included == ("vat", "tax") and entry.currency == "EUR"

    # The person disables the sensor: the registry tells, and the area has no source.
    er.async_get(world.hass).async_update_entity(world.connection.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    await world.hass.async_block_till_done()
    assert world.repository.provider_for(AREA) is None and world.heard == [1, 1]

    er.async_get(world.hass).async_update_entity(world.connection.entity_id, disabled_by=None)
    await world.hass.async_block_till_done()
    assert world.repository.provider_for(AREA) is world.provider and world.heard == [1, 1, 1]

    world.entry.mock_state(world.hass, ConfigEntryState.NOT_LOADED)
    assert world.repository.provider_for(AREA) is None and world.heard == [1, 1, 1, 1]


async def test_a_subscribed_area_is_read_and_waits_for_the_afternoon(world: World) -> None:
    await world.start()
    await world.publish(instants(TODAY))

    await world.manager.async_subscribe(owner_id="charger", area_id=AREA)
    await world.hass.async_block_till_done()

    assert world.manager.area_snapshot(AREA).today_state == "ready"
    assert world.repository.file_authority(AREA, TOMORROW) == "not_listed"

    world.clock.now = datetime(2026, 10, 4, 12, 30, tzinfo=UTC)
    await world.publish(instants(TODAY) + instants(TOMORROW))
    await world.manager.async_refresh_area(AREA)
    await world.hass.async_block_till_done()
    assert world.manager.area_snapshot(AREA).tomorrow_state == "ready"
    # Its days are its own: the spot area NL holds none of them.
    assert world.repository.holds_file(AREA, TODAY) and not world.repository.holds_file("NL", TODAY)


async def test_setup_builds_the_zonneplan_provider(hass: HomeAssistant) -> None:
    from custom_components.spotnav.pricing.price_repository import async_setup_price_repository

    await hass.config.async_set_time_zone(AMSTERDAM)
    entry = MockConfigEntry(domain="zonneplan_one", title="Zonneplan", data={})
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    repository = await async_setup_price_repository(hass, store=StoreDouble())
    try:
        assert repository.provider_for(AREA) is None
        Connection(hass, entry)
        await hass.async_block_till_done()
        assert repository.provider_for(AREA).key == "zonneplan"
    finally:
        repository.stop_following_providers()
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


# ------------------------------------------------------------------ the notice


async def test_the_notice_follows_the_entry_and_the_sensor(world: World) -> None:
    await world.start(sensor=False)
    assert world.notice(AREA) == (None, "zonneplan_no_tariff")
    Connection(world.hass, world.entry)
    await world.hass.async_block_till_done()
    assert world.notice(AREA) == ("zonneplan", None)
    status = world.repository.source_status(AREA)
    assert (status.name, status.url) == ("Zonneplan", "https://www.zonneplan.nl/energie")
    world.entry.mock_state(world.hass, ConfigEntryState.SETUP_ERROR)
    await world.hass.async_block_till_done()
    assert world.notice(AREA) == (None, "zonneplan_not_loaded")


async def test_without_the_integration_the_notice_asks_for_it(world: World) -> None:
    await world.start(loaded=False)
    await world.hass.config_entries.async_remove(world.entry.entry_id)
    assert world.notice(AREA) == (None, "add_zonneplan")
    assert world.notice("NL") == (None, "add_nordpool")


async def test_nl_hints_at_the_zonneplan_area_while_the_integration_is_loaded(world: World) -> None:
    await world.start(loaded=False)
    assert world.notice("NL") == (None, "add_nordpool")
    # Zonneplan loaded, but NL has no source of its own: NL's own notice first, the one its Repairs issue names.
    world.entry.mock_state(world.hass, ConfigEntryState.LOADED)
    assert world.notice("NL") == (None, "add_nordpool")

    nordpool = MockConfigEntry(domain="nordpool", title="Nord Pool", data={})
    nordpool.add_to_hass(world.hass)
    nordpool.mock_state(world.hass, ConfigEntryState.LOADED)
    try:
        # NL has prices of its own: now the hint, beside the source.
        assert world.notice("NL") == ("nordpool", "zonneplan_area")
        # Only the Netherlands' other area: Belgium and Spain's PVPC are not Zonneplan's.
        assert world.notice("BE") == ("nordpool", None)
        assert world.notice("ES-PVPC") == (None, "add_pvpc")
        world.entry.mock_state(world.hass, ConfigEntryState.NOT_LOADED)
        assert world.notice("NL") == ("nordpool", None)
    finally:
        nordpool.mock_state(world.hass, ConfigEntryState.NOT_LOADED)


# ------------------------------------------------------------------ the app's price days


async def test_the_apps_price_days_say_the_files_are_all_in_and_from_zonneplan(world: World) -> None:
    from custom_components.spotnav.api.price_days import async_price_days_answer, PriceDaysRefusal

    await world.start()
    await world.publish(instants(TODAY))
    await world.repository.async_get_file(AREA, TODAY)

    answer = await async_price_days_answer(world.repository, AREA, world.clock.now)

    assert not isinstance(answer, PriceDaysRefusal)
    assert answer["area"]["included"] == ["vat", "tax"] and "vat_percent" not in answer["area"]
    assert (answer["provider"]["key"], answer["provider"]["name"]) == ("zonneplan", "Zonneplan")
    assert list(answer["files"]) == ["2026-10-04"]
    assert answer["files"]["2026-10-04"]["prices"] == expected(instants(TODAY))
