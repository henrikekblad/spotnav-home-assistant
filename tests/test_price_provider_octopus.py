"""Great Britain's Agile rates through the Octopus Energy integration (HACS), from recorded entity states.

The integration's three rate event entities per meter are registered and given the states it writes
(`coordinators/__init__.py` `raise_rate_events`, `utils/__init__.py` `private_rates_to_public_rates`): `rates`
as `{"start", "end", "value_inc_vat"}` with aware datetimes in Home Assistant's zone and pounds per kWh, split
into previous, current and next calendar days of that zone, plus `tariff_code`, `mpan` and `serial_number`. The
rates are the relay's own GB-C days (`tests/fixtures/relay_v2/`) turned back into pounds with their rate, so
the documents built here must carry the relay's numbers. No network.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
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
from custom_components.spotnav.pricing.providers import DayFetch, ProviderRouter
from custom_components.spotnav.pricing.providers.octopus import agile_region, OctopusProvider, PUBLICATION
from custom_components.spotnav.pricing.relay_contract import parse_day

from .relay import Clock, FakeScheduler, StoreDouble, StubTransport

UTC = timezone.utc
LONDON = "Europe/London"
PARIS = "Europe/Paris"
RELAY_FILES = Path(__file__).parent / "fixtures" / "relay_v2"
AREA = "GB-C"
AGILE_C = "E-1R-AGILE-24-10-01-C"
SERIAL = "21L1234567"
MPAN = "1900001234567"

#: 13:00 in London on 2026-10-04: today's Agile day is out, tomorrow's not before 16:00.
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
SLOT = timedelta(minutes=30)


def relay_file(day: date) -> dict[str, Any]:
    return json.loads((RELAY_FILES / f"GB-C_{day.isoformat()}.json").read_text(encoding="utf-8"))


def relay_slots(day: date) -> dict[datetime, float]:
    """A relay GB-C day as the rates Octopus published: start instant → pounds per kWh."""
    document = relay_file(day)
    rate = document["fx"]["GBP"]
    start = datetime.fromisoformat(document["start"]).astimezone(UTC)
    return {start + SLOT * index: round(price * rate, 6) for index, price in enumerate(document["prices"])}


def public_rates(slots: dict[datetime, float], *, as_text: bool = False) -> list[dict[str, Any]]:
    """`private_rates_to_public_rates`: local aware datetimes, pounds; ISO text as a restored state has it."""
    zone = ZoneInfo(LONDON)
    rows = []
    for start in sorted(slots):
        row = {"start": start.astimezone(zone), "end": (start + SLOT).astimezone(zone), "value_inc_vat": slots[start]}
        if as_text:
            row = {**row, "start": row["start"].isoformat(), "end": row["end"].isoformat()}
        rows.append(row)
    return rows


class FakeRates:
    def __init__(self, rate: float | None, day: date) -> None:
        self.rate = rate
        self.day = day
        self.refreshed = 0

    def rate_for(self, currency: str, day: date) -> tuple[float, date] | None:
        return None if self.rate is None or currency != "GBP" else (self.rate, self.day)

    async def async_start(self) -> None:
        return None

    async def async_refresh(self) -> bool:
        self.refreshed += 1
        return False


class Meter:
    """One electricity meter's three rate event entities, registered as the integration registers them."""

    def __init__(self, hass: HomeAssistant, entry: MockConfigEntry, serial: str, mpan: str, *, export: bool = False):
        self.hass = hass
        self.serial = serial
        self.mpan = mpan
        registry = er.async_get(hass)
        middle = f"{serial}_{mpan}{'_export' if export else ''}"
        self.entity_ids: dict[str, str] = {}
        for kind in ("previous_day_rates", "current_day_rates", "next_day_rates"):
            unique_id = f"octopus_energy_electricity_{middle}_{kind}"
            registered = registry.async_get_or_create(
                "event", "octopus_energy", unique_id, config_entry=entry, suggested_object_id=f"renamed_{kind}_{mpan}"
            )
            self.entity_ids[kind] = registered.entity_id

    def publish(
        self,
        slots: dict[datetime, float],
        now: datetime,
        *,
        tariff: str = AGILE_C,
        as_text: bool = False,
        tariffs: dict[str, str] | None = None,
    ) -> None:
        """Write the three states as the coordinator does at [now]: rates from yesterday (Home Assistant's zone)
        on, split by calendar day."""
        zone = ZoneInfo(LONDON)
        today = now.astimezone(zone).date()
        split: dict[str, dict[datetime, float]] = {"previous_day_rates": {}, "current_day_rates": {}, "next_day_rates": {}}
        for start, value in slots.items():
            local = start.astimezone(zone).date()
            if local == today - timedelta(days=1):
                split["previous_day_rates"][start] = value
            elif local == today:
                split["current_day_rates"][start] = value
            elif local == today + timedelta(days=1):
                split["next_day_rates"][start] = value
        for kind, entity_id in self.entity_ids.items():
            rates = public_rates(split[kind], as_text=as_text)
            values = [row["value_inc_vat"] for row in rates] or [0.0]
            self.hass.states.async_set(
                entity_id,
                now.isoformat(),
                {
                    "event_type": f"octopus_energy_electricity_{kind}",
                    "rates": rates,
                    "min_rate": min(values),
                    "max_rate": max(values),
                    "average_rate": sum(values) / len(values),
                    "mpan": self.mpan,
                    "serial_number": self.serial,
                    "tariff_code": (tariffs or {}).get(kind, tariff),
                },
            )


class World:
    def __init__(self, hass: HomeAssistant, clock: Clock, transport: StubTransport) -> None:
        self.hass = hass
        self.clock = clock
        self.transport = transport
        self.entry = MockConfigEntry(domain="octopus_energy", title="Octopus Energy", data={"account_id": "A-1"})
        self.rates = FakeRates(0.8712, date(2026, 10, 2))
        self.scheduler = FakeScheduler()
        self.store = StoreDouble()

    async def start(self, *, loaded: bool = True) -> None:
        await self.hass.config.async_set_time_zone(LONDON)
        await time_zones.async_resolve([LONDON, PARIS])
        self.entry.add_to_hass(self.hass)
        if loaded:
            self.entry.mock_state(self.hass, ConfigEntryState.LOADED)
        self.meter = Meter(self.hass, self.entry, SERIAL, MPAN)
        bundled = await async_load_bundled(self.hass)
        assert bundled is not None
        self.provider = OctopusProvider(self.hass, bundled, self.rates, now=self.clock)
        self.router = ProviderRouter(
            octopus=self.provider
        )
        self.router.async_start(self.hass)
        self.repository = PriceRepository(
            self.hass, now=self.clock, store=self.store, bundled=bundled, router=self.router
        )
        self.manager = PriceRefreshManager(
            self.hass, self.repository, now=self.clock, jitter=lambda: 0.5, scheduler=self.scheduler
        )

    async def publish(self, *days: date, meter: Meter | None = None, **kwargs: Any) -> None:
        slots: dict[datetime, float] = {}
        for day in days:
            slots.update(relay_slots(day))
        (meter or self.meter).publish(slots, self.clock.now, **kwargs)
        await self.hass.async_block_till_done()

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


TODAY = date(2026, 10, 4)
TOMORROW = date(2026, 10, 5)


# ------------------------------------------------------------------ the document


async def test_todays_paris_day_is_the_relays_gb_file(world: World) -> None:
    await world.start()
    await world.publish(TODAY)

    fetch = await world.provider.async_fetch_day(AREA, TODAY)

    assert fetch.outcome == "fetched"
    document, relay = fetch.document, relay_file(TODAY)
    assert document["v"] == 1 and document["area"] == AREA and document["date"] == "2026-10-04"
    assert document["tz"] == LONDON and document["market_tz"] == PARIS
    # 23:00 London the evening before: the Paris day, as the relay dates it.
    assert document["start"] == relay["start"] == "2026-10-04T00:00:00+02:00"
    assert document["res"] == 30 and document["unit"] == "EUR/kWh"
    assert document["prices"] == relay["prices"]
    assert document["fx"] == {"GBP": 0.8712} and document["fx_date"] == "2026-10-02" and document["fx_src"] == "ECB"
    assert document["src"] == "octopus.energy"
    parsed = parse_day(document, area_id=AREA, day=TODAY)
    assert parsed.interval_count == 48 and parsed.covers_whole_day()


async def test_tomorrow_is_not_published_before_octopus_and_whole_after(world: World) -> None:
    await world.start()
    await world.publish(TODAY)
    assert await world.provider.async_fetch_day(AREA, TOMORROW) == DayFetch("not_published", code="not_published")

    # 16:10 in London: the integration has the next day's rates, 23:00 tonight to 23:00 tomorrow.
    world.clock.now = datetime(2026, 10, 4, 15, 10, tzinfo=UTC)
    await world.publish(TODAY, TOMORROW)
    fetch = await world.provider.async_fetch_day(AREA, TOMORROW)
    assert fetch.outcome == "fetched" and fetch.document["prices"] == relay_file(TOMORROW)["prices"]


async def test_a_day_half_published_is_not_published_yet(world: World) -> None:
    await world.start()
    world.clock.now = datetime(2026, 10, 4, 15, 10, tzinfo=UTC)
    slots = {**relay_slots(TODAY), **dict(list(relay_slots(TOMORROW).items())[:20])}
    world.meter.publish(slots, world.clock.now)
    assert await world.provider.async_fetch_day(AREA, TOMORROW) == DayFetch("not_published", code="not_published")


@pytest.mark.parametrize(
    ("day", "now", "slots", "rate"),
    [
        (date(2026, 10, 25), datetime(2026, 10, 25, 12, 0, tzinfo=UTC), 50, (0.8698, date(2026, 10, 23))),
        (date(2026, 3, 29), datetime(2026, 3, 29, 12, 0, tzinfo=UTC), 46, (0.8455, date(2026, 3, 27))),
    ],
)
async def test_clock_change_days_keep_every_half_hour(world: World, day, now, slots: int, rate) -> None:
    world.clock.now = now
    world.rates.rate, world.rates.day = rate
    await world.start()
    await world.publish(day)

    fetch = await world.provider.async_fetch_day(AREA, day)

    assert fetch.outcome == "fetched"
    assert len(fetch.document["prices"]) == slots and fetch.document["prices"] == relay_file(day)["prices"]
    parsed = parse_day(fetch.document, area_id=AREA, day=day)
    assert parsed.interval_count == slots and parsed.covers_whole_day()


async def test_the_day_after_the_autumn_change_is_cut_across_the_uk_days(world: World) -> None:
    """Paris 2026-10-26 runs from 23:00 GMT on the 25th: its first two slots sit in the current UK day's rates."""
    world.clock.now = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)
    world.rates.rate, world.rates.day = 0.8698, date(2026, 10, 23)
    await world.start()
    await world.publish(date(2026, 10, 25), date(2026, 10, 26))

    current = world.hass.states.get(world.meter.entity_ids["current_day_rates"]).attributes["rates"]
    assert current[-1]["start"] == datetime(2026, 10, 25, 23, 30, tzinfo=ZoneInfo(LONDON))
    fetch = await world.provider.async_fetch_day(AREA, date(2026, 10, 26))
    assert fetch.outcome == "fetched"
    assert fetch.document["start"] == "2026-10-26T00:00:00+01:00"
    assert fetch.document["prices"] == relay_file(date(2026, 10, 26))["prices"]


async def test_a_restored_state_with_text_instants_is_read_the_same(world: World) -> None:
    await world.start()
    await world.publish(TODAY, as_text=True)
    assert (await world.provider.async_fetch_day(AREA, TODAY)).document["prices"] == relay_file(TODAY)["prices"]


# ------------------------------------------------------------------ not there, refused


async def test_yesterdays_paris_day_is_out_of_reach(world: World) -> None:
    """The integration keeps rates from midnight (UK) yesterday: an hour short of yesterday's Paris day."""
    await world.start()
    world.clock.now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    await world.publish(TODAY, TOMORROW)

    assert await world.provider.async_fetch_day(AREA, TODAY) == DayFetch("failed", code="octopus_unavailable")
    assert await world.repository.async_get_archive_day(AREA, date(2026, 10, 3)) is None
    assert (await world.provider.async_fetch_day(AREA, TOMORROW)).outcome == "fetched"


async def test_today_without_its_first_hour_is_unavailable(world: World) -> None:
    await world.start()
    slots = dict(list(relay_slots(TODAY).items())[2:])
    world.meter.publish(slots, world.clock.now)
    assert await world.provider.async_fetch_day(AREA, TODAY) == DayFetch("failed", code="octopus_unavailable")


async def test_without_a_rate_the_day_is_refused_and_the_ecb_asked(world: World) -> None:
    world.rates.rate = None
    await world.start()
    await world.publish(TODAY)
    assert await world.provider.async_fetch_day(AREA, TODAY) == DayFetch("failed", code="fx_missing")
    assert world.rates.refreshed == 1


def _set_value(value: Any):
    def damage(rates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rates[3] = {**rates[3], "value_inc_vat": value}
        return rates

    return damage


@pytest.mark.parametrize(
    "damage",
    [
        _set_value("0.25"),
        _set_value(None),
        _set_value(math.inf),
        _set_value(True),
        _set_value(250.0),
        lambda rates: [{**rates[0], "end": rates[0]["start"] + timedelta(hours=1)}, *rates[1:]],
        lambda rates: [{key: value for key, value in rates[0].items() if key != "end"}, *rates[1:]],
        lambda rates: [{**rates[0], "start": "yesterday"}, *rates[1:]],
        lambda rates: [{**rates[0], "start": rates[0]["start"].replace(tzinfo=None)}, *rates[1:]],
        lambda rates: rates[:5] + rates[6:],
        lambda rates: {"rates": rates},
        lambda rates: [*rates, "a rate"],
    ],
)
async def test_rates_this_client_cannot_read_are_refused(world: World, damage) -> None:
    await world.start()
    await world.publish(TODAY)
    entity_id = world.meter.entity_ids["current_day_rates"]
    state = world.hass.states.get(entity_id)
    world.hass.states.async_set(
        entity_id, state.state, {**state.attributes, "rates": damage([dict(row) for row in state.attributes["rates"]])}
    )
    assert await world.provider.async_fetch_day(AREA, TODAY) == DayFetch("failed", code="provider_refused")


async def test_two_entities_disagreeing_about_a_slot_are_refused(world: World) -> None:
    await world.start()
    await world.publish(TODAY)
    current = world.hass.states.get(world.meter.entity_ids["current_day_rates"])
    first = dict(current.attributes["rates"][0])
    previous_id = world.meter.entity_ids["previous_day_rates"]
    previous = world.hass.states.get(previous_id)
    world.hass.states.async_set(
        previous_id,
        previous.state,
        {**previous.attributes, "rates": [*previous.attributes["rates"], {**first, "value_inc_vat": 9.9}]},
    )
    assert await world.provider.async_fetch_day(AREA, TODAY) == DayFetch("failed", code="provider_refused")


async def test_yesterdays_rates_of_another_tariff_are_left_out(world: World) -> None:
    """The day the customer moved to Agile: yesterday's rates are another tariff's and not read."""
    await world.start()
    await world.publish(TODAY, tariffs={"previous_day_rates": "E-1R-VAR-22-11-01-C"})
    assert await world.provider.async_fetch_day(AREA, TODAY) == DayFetch("failed", code="octopus_unavailable")


# ------------------------------------------------------------------ the meter and its region


@pytest.mark.parametrize(
    ("code", "region"),
    [
        ("E-1R-AGILE-24-10-01-C", "C"),
        ("E-1R-AGILE-FLEX-22-11-25-P", "P"),
        ("E-1R-AGILE-OUTGOING-19-05-13-C", None),
        ("E-1R-VAR-22-11-01-C", None),
        ("E-1R-INTELLI-VAR-22-10-14-C", None),
        ("E-2R-AGILE-24-10-01-C", None),
        ("G-1R-AGILE-24-10-01-C", None),
        (None, None),
    ],
)
def test_only_an_agile_import_tariff_names_a_region(code, region) -> None:
    assert agile_region(code) == region


async def test_the_meter_is_found_by_unique_id_whatever_its_entity_id(world: World) -> None:
    await world.start()
    await world.publish(TODAY)
    assert world.meter.entity_ids["current_day_rates"] == f"event.renamed_current_day_rates_{MPAN}"
    assert [meter.region for meter in world.provider.meters] == ["C"]
    assert world.provider.serves(AREA) and world.provider.region_mismatch(AREA) is None


async def test_an_export_meter_or_another_tariff_serves_nothing(world: World) -> None:
    await world.start()
    await world.publish(TODAY, tariff="E-1R-VAR-22-11-01-C")
    exporter = Meter(world.hass, world.entry, "21E7654321", "1170000000001", export=True)
    await world.publish(TODAY, meter=exporter, tariff="E-1R-AGILE-OUTGOING-19-05-13-C")
    assert world.provider.meters == () and not world.provider.serves(AREA)
    assert world.repository.provider_for(AREA) is None


async def test_another_region_than_the_meters_is_read_from_the_meter(world: World) -> None:
    """The person chose London but their meter is in Eastern England: their own rates are what they pay."""
    await world.start()
    await world.publish(TODAY, tariff="E-1R-AGILE-24-10-01-A")
    assert world.provider.serves("GB-C") and world.provider.region_mismatch("GB-C") == "A"
    assert world.provider.region_mismatch("GB-A") is None
    fetch = await world.provider.async_fetch_day("GB-C", TODAY)
    assert fetch.outcome == "fetched" and fetch.document["area"] == "GB-C"


async def test_meters_in_several_regions_serve_only_their_own(world: World) -> None:
    await world.start()
    await world.publish(TODAY, tariff="E-1R-AGILE-24-10-01-A")
    second = Meter(world.hass, world.entry, "22L0000002", "1900009999999")
    await world.publish(TODAY, meter=second)
    assert sorted(meter.region for meter in world.provider.meters) == ["A", "C"]
    assert world.provider.meter_for("GB-C").region == "C" and world.provider.meter_for("GB-A").region == "A"
    assert not world.provider.serves("GB-B")


async def test_a_disabled_entity_or_an_unloaded_entry_serves_nothing(world: World) -> None:
    await world.start(loaded=False)
    await world.publish(TODAY)
    assert world.repository.provider_for(AREA) is None

    world.entry.mock_state(world.hass, ConfigEntryState.LOADED)
    assert world.repository.provider_for(AREA) is world.provider

    # Disabling an entity removes it, which is a state change of its own; here the registry alone is changed.
    er.async_get(world.hass).async_update_entity(
        world.meter.entity_ids["current_day_rates"], disabled_by=er.RegistryEntryDisabler.USER
    )
    assert world.provider.refresh() is True
    assert world.repository.provider_for(AREA) is None


# ------------------------------------------------------------------ the choice and the schedule


async def test_the_router_follows_the_meters_rate_entities(world: World) -> None:
    await world.start()
    heard: list[int] = []
    world.repository.add_provider_listener(lambda: heard.append(1))
    assert world.repository.provider_for(AREA) is None

    await world.publish(TODAY)
    assert world.repository.provider_for(AREA) is world.provider and heard == [1]
    entry = world.repository.area_entry(AREA)
    assert entry.market_tz == PARIS and entry.tz == LONDON and entry.publication == PUBLICATION
    assert entry.included == ("vat", "tax", "grid_fee") and entry.currency == "GBP"

    # The same meter's rates again: nothing to tell.
    await world.publish(TODAY)
    assert heard == [1]

    # The customer leaves Agile.
    await world.publish(TODAY, tariff="E-1R-VAR-22-11-01-C")
    assert world.repository.provider_for(AREA) is None and heard == [1, 1]

    await world.publish(TODAY)
    world.entry.mock_state(world.hass, ConfigEntryState.NOT_LOADED)
    assert world.repository.provider_for(AREA) is None and heard == [1, 1, 1, 1]


async def test_a_subscribed_area_is_read_and_waits_for_the_afternoon_publication(world: World) -> None:
    await world.start()
    await world.publish(TODAY)

    await world.manager.async_subscribe(owner_id="charger", area_id=AREA)
    await world.hass.async_block_till_done()

    assert world.manager.area_snapshot(AREA).today_state == "ready"
    assert world.repository.file_authority(AREA, TOMORROW) == "not_listed"

    world.clock.now = datetime(2026, 10, 4, 15, 10, tzinfo=UTC)
    await world.publish(TODAY, TOMORROW)
    await world.manager.async_refresh_area(AREA)
    await world.hass.async_block_till_done()
    assert world.manager.area_snapshot(AREA).tomorrow_state == "ready"


async def test_setup_builds_the_octopus_provider(hass: HomeAssistant) -> None:
    from custom_components.spotnav.pricing.price_repository import async_setup_price_repository

    await hass.config.async_set_time_zone(LONDON)
    entry = MockConfigEntry(domain="octopus_energy", title="Octopus Energy", data={})
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    repository = await async_setup_price_repository(hass, store=StoreDouble())
    try:
        assert repository.provider_for(AREA) is None
        meter = Meter(hass, entry, SERIAL, MPAN)
        meter.publish(relay_slots(TODAY), NOW)
        await hass.async_block_till_done()
        assert repository.provider_for(AREA).key == "octopus"
    finally:
        repository.stop_following_providers()
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


# ------------------------------------------------------------------ the notice


def _notice(world: World, area_id: str) -> tuple[str | None, Any]:
    status = world.repository.source_status(area_id)
    return status.key, None if status.notice is None else status.notice.as_dict()


async def test_the_notice_follows_the_meter_and_the_entry(world: World) -> None:
    await world.start()
    # Loaded, but no Agile meter's rates yet.
    assert _notice(world, "GB-C") == (None, {"code": "octopus_no_agile", "params": {}})
    await world.publish(TODAY)
    assert _notice(world, "GB-C") == ("octopus", None)
    status = world.repository.source_status("GB-C")
    assert (status.name, status.url) == ("Octopus Energy (Agile)", "https://octopus.energy/smart/agile/")
    # Another region than the meter's: read from the meter, and said.
    assert _notice(world, "GB-A") == ("octopus", {"code": "octopus_region_mismatch", "params": {"region": "C"}})
    world.entry.mock_state(world.hass, ConfigEntryState.SETUP_ERROR)
    await world.hass.async_block_till_done()
    assert _notice(world, "GB-C") == (None, {"code": "octopus_not_loaded", "params": {}})


async def test_without_the_integration_the_notice_asks_for_it(world: World) -> None:
    await world.start(loaded=False)
    await world.hass.config_entries.async_remove(world.entry.entry_id)
    assert _notice(world, "GB-C") == (None, {"code": "add_octopus", "params": {}})
