"""The repository's store, schema 2: day files keyed by market calendar, kept for a bounded time.

A schema 1 store (what every install wrote before) must come through the migration with every day it
held, read again by the same parser; files of one area dated on two calendars (Finland's relay files are
Helsinki days, a CET source's are Brussels days) are held side by side and only the one the area is read
in is shown; files older than the retention are dropped on the next write.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant

from custom_components.spotnav import time_zones
from custom_components.spotnav.pricing.area_catalogue import BUNDLED_PATH, parse_bundled
from custom_components.spotnav.pricing.price_repository import (
    HISTORY_KEEP_DAYS,
    PriceRepository,
    STORAGE_SCHEMA,
)
from .relay import BASE_URL, Clock, StoreDouble, StubTransport, TODAY, day_body, fixture

STAMP = "2026-09-22T04:00:00+00:00"


def stored(document: dict[str, Any], stamp: str = STAMP, **extra: Any) -> dict[str, Any]:
    return {"document": document, "fetched_at": stamp, **extra}


def finnish_file(day: date, calendar: str) -> dict[str, Any]:
    """A Finnish day file dated on [calendar]: the relay's Helsinki day, or a CET day shown in Helsinki."""
    document = json.loads(day_body("FI", day))
    document["tz"] = "Europe/Helsinki"
    if calendar != "Europe/Helsinki":
        document["market_tz"] = calendar
    document["start"] = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(calendar)).isoformat()
    return document


async def bundled(*, finland_on_cet: bool = False):
    """The bundled catalogue, or a copy that reads Finland on the CET calendar (as a CET source will)."""
    document = json.loads(BUNDLED_PATH.read_text(encoding="utf-8"))
    if finland_on_cet:
        for area in document["areas"]:
            if area["id"] == "FI":
                area["market_tz"] = area["routes"]["cet_market_tz"]
    await time_zones.async_resolve_document(document)
    await time_zones.async_resolve(["Europe/Brussels"])
    return parse_bundled(document)


def schema_one() -> dict[str, Any]:
    """A store as the release before wrote it: relay resources at the top, `AREA|date` day keys."""
    days = {
        "SE4|2026-09-22": stored(json.loads(day_body("SE4", TODAY))),
        "SE4|2026-09-21": stored(json.loads(day_body("SE4", TODAY - timedelta(days=1)))),
        "SE4|2026-03-29": stored(json.loads(fixture("day_SE4_2026-03-29_92.json"))),
        "SE4|2025-10-26": stored(json.loads(fixture("day_SE4_2025-10-26_100.json"))),
        "SE4|2025-09-30": stored(json.loads(fixture("day_SE4_2025-09-30_hourly.json"))),
        "FI|2026-09-22": stored(finnish_file(TODAY, "Europe/Helsinki")),
    }
    return {
        "schema": 1,
        "catalogue": stored(json.loads(fixture("areas.json"))),
        "catalogue_rev": "916f2f8e05fd",
        "index": stored(json.loads(fixture("index.json"))),
        "days": days,
        "profiles": {"SE4": stored(json.loads(fixture("profile_SE4.json")))},
    }


async def test_a_schema_one_store_migrates_with_every_day_it_held(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    legacy = schema_one()
    store = StoreDouble(copy.deepcopy(legacy))
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=store)
    await repository.async_restore()

    # Every day is read back by the same parser, nothing fetched.
    for key in legacy["days"]:
        area, _, raw_day = key.partition("|")
        snapshot = repository.file_snapshot(area, date.fromisoformat(raw_day))
        assert snapshot.document is not None and snapshot.source == "store", key
    assert repository.catalogue_snapshot().catalogue is not None
    assert repository.index_snapshot().index is not None
    assert repository.catalogue_revision() == "916f2f8e05fd"
    assert transport.calls == []

    # The next write is schema 2: every document unchanged, filed under its own calendar.
    await repository._persist()
    written = store.payload
    assert written["schema"] == STORAGE_SCHEMA == 2
    calendars = {"SE4": "Europe/Stockholm", "FI": "Europe/Helsinki"}
    expected = {
        f"{key.split('|')[0]}|{calendars[key.split('|')[0]]}|{key.split('|')[1]}": entry
        for key, entry in legacy["days"].items()
    }
    assert set(written["days"]) == set(expected)
    for key, entry in expected.items():
        assert written["days"][key] == {**entry, "provider": "relay"}, key
    relay = written["relay"]
    assert relay["catalogue"] == legacy["catalogue"]
    assert relay["catalogue_rev"] == "916f2f8e05fd"
    assert relay["index"] == legacy["index"]
    # The relay's profile is not kept: the profile is built from the held days.
    assert "profiles" not in relay
    assert set(written) == {"schema", "days", "relay"}

    # And schema 2 restores to the same thing in a new process.
    again = PriceRepository(hass, base_url=BASE_URL, session=StubTransport(), now=clock, store=StoreDouble(written))
    await again.async_restore()
    for key in legacy["days"]:
        area, _, raw_day = key.partition("|")
        before = repository.file_snapshot(area, date.fromisoformat(raw_day))
        after = again.file_snapshot(area, date.fromisoformat(raw_day))
        assert after.document is not None and after.document.prices == before.document.prices, key
        assert after.fetched_at == before.fetched_at


async def test_a_fetched_file_is_stored_under_its_calendar_with_its_provider(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    transport.serve_area()
    store = StoreDouble()
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=store)
    await repository.async_get_day("SE4", TODAY)

    entry = store.payload["days"]["SE4|Europe/Stockholm|2026-09-22"]
    assert entry["provider"] == "relay"
    assert entry["document"] == json.loads(day_body("SE4", TODAY))


async def test_files_older_than_the_retention_are_dropped_on_the_next_write(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    oldest_kept = TODAY - timedelta(days=HISTORY_KEEP_DAYS)
    too_old = oldest_kept - timedelta(days=1)
    store = StoreDouble(
        {
            "schema": 2,
            "days": {
                f"SE4|Europe/Stockholm|{day.isoformat()}": stored(json.loads(day_body("SE4", day)))
                for day in (too_old, oldest_kept, TODAY)
            },
        }
    )
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=store)
    await repository.async_restore()
    # Restoring keeps everything; nothing is lost before a write.
    assert repository.file_snapshot("SE4", too_old).document is not None

    await repository._persist()
    assert set(store.payload["days"]) == {
        f"SE4|Europe/Stockholm|{oldest_kept.isoformat()}",
        "SE4|Europe/Stockholm|2026-09-22",
    }
    assert repository.file_snapshot("SE4", too_old).document is None
    assert repository.file_snapshot("SE4", oldest_kept).document is not None

    # The window moves with the clock.
    clock.advance(days=1)
    await repository._persist()
    assert set(store.payload["days"]) == {"SE4|Europe/Stockholm|2026-09-22"}


async def test_finnish_files_on_two_calendars_coexist_and_only_the_area_calendar_is_read(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    helsinki = finnish_file(TODAY, "Europe/Helsinki")
    cet = finnish_file(TODAY, "Europe/Brussels")
    cet["prices"] = [0.5] * len(cet["prices"])
    payload = {
        "schema": 2,
        "days": {
            "FI|Europe/Helsinki|2026-09-22": stored(helsinki),
            "FI|Europe/Brussels|2026-09-22": stored(cet, "2026-09-22T05:00:00+00:00", provider="nordpool"),
        },
    }

    # Read on the relay's calendar (the bundled list says Helsinki): the Helsinki file, though the CET one is newer.
    relay_era = PriceRepository(
        hass, base_url=BASE_URL, session=transport, now=clock, store=StoreDouble(copy.deepcopy(payload)),
        bundled=await bundled(),
    )
    await relay_era.async_restore()
    held = relay_era.file_snapshot("FI", TODAY)
    assert held.document is not None and held.document.tz == "Europe/Helsinki"
    assert relay_era.day_snapshot("FI", TODAY).document.prices == tuple(helsinki["prices"])

    # Read on the CET calendar: the CET file, and the Helsinki one is not mistaken for it.
    store = StoreDouble(copy.deepcopy(payload))
    switched = PriceRepository(
        hass, base_url=BASE_URL, session=transport, now=clock, store=store,
        bundled=await bundled(finland_on_cet=True),
    )
    await switched.async_restore()
    principal = switched.file_snapshot("FI", TODAY)
    assert principal.document is not None and principal.document.tz == "Europe/Brussels"
    assert switched.file_snapshot("FI", TODAY - timedelta(days=1)).document is None

    # Both stay held, each under its own key and with its own provider.
    await switched._persist()
    assert store.payload["days"]["FI|Europe/Helsinki|2026-09-22"]["provider"] == "relay"
    assert store.payload["days"]["FI|Europe/Brussels|2026-09-22"]["provider"] == "nordpool"
    assert transport.calls == []


async def test_a_file_dated_on_another_calendar_than_the_area_is_refused_when_fetched(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    # No index to say what exists, so the file is asked for.
    transport.serve("/v1/index.json", 503, "busy")
    transport.serve(transport.day_path("FI", TODAY), 200, json.dumps(finnish_file(TODAY, "Europe/Brussels")))
    store = StoreDouble()
    repository = PriceRepository(
        hass, base_url=BASE_URL, session=transport, now=clock, store=store, bundled=await bundled()
    )

    snapshot = await repository.async_get_file("FI", TODAY)

    # It could never be found by a lookup on the Helsinki calendar, so it is not taken at all.
    assert snapshot.document is None and snapshot.attempt_error == "invalid_coverage"
    assert store.payload is None


async def test_a_stored_file_whose_key_names_another_calendar_is_ignored(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    store = StoreDouble(
        {
            "schema": 2,
            "days": {
                "SE4|Europe/Helsinki|2026-09-22": stored(json.loads(day_body("SE4", TODAY))),
                "SE4|2026-09-22": stored(json.loads(day_body("SE4", TODAY))),
                "SE4|Europe/Stockholm|not-a-date": stored(json.loads(day_body("SE4", TODAY))),
            },
        }
    )
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=store)
    await repository.async_restore()
    assert repository.file_snapshot("SE4", TODAY).document is None


async def test_a_store_of_an_unknown_schema_is_ignored(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    store = StoreDouble(
        {"schema": 3, "days": {"SE4|Europe/Stockholm|2026-09-22": stored(json.loads(day_body("SE4", TODAY)))}}
    )
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=store)
    await repository.async_restore()
    assert repository.file_snapshot("SE4", TODAY).document is None


async def test_a_day_file_dated_in_an_equal_zone_is_found_under_the_area_calendar(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    """Calendars compare by instant: a Stockholm-dated file is a Berlin day too, so a German area finds it."""
    transport.serve_area()
    transport.serve("/v1/DE-LU/2026/09-22.json", 200, day_body("DE-LU", TODAY))
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=StoreDouble())
    await repository.async_get_catalogue()

    snapshot = await repository.async_get_file("DE-LU", TODAY)

    assert snapshot.document is not None and snapshot.state == "ready"
    assert repository.file_snapshot("DE-LU", TODAY).document is snapshot.document
    assert datetime(2026, 9, 21, 22, tzinfo=timezone.utc) == snapshot.document.start_instant
