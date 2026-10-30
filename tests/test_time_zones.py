"""Time zones are resolved off the event loop and read from a cache on it.

Every test patches the blocking lookup (`dt_util.get_time_zone`, which builds a `ZoneInfo` from disk) to
fail, after which nothing on the loop may call it: a fetched or restored document has had its zones
resolved before it is parsed, and a name nobody resolved reads as not usable yet instead of blocking.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav import time_zones
from custom_components.spotnav.pricing.area_catalogue import async_load_bundled
from custom_components.spotnav.planning.price_wait import PUBLICATION_ZONE
from custom_components.spotnav.pricing.market_day import display_day_span, market_day_of
from custom_components.spotnav.pricing.price_refresh import in_publication_window, local_dates
from custom_components.spotnav.pricing.relay_contract import DEFAULT_PUBLICATION

from .relay import stub_repository, Clock, StoreDouble, StubTransport, TODAY

ZONES = ("Europe/Stockholm", "Europe/Berlin", "Europe/Copenhagen", "Europe/Oslo")


class BlockingLookup(AssertionError):
    pass


@pytest.fixture
def no_blocking_lookup(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make the disk-reading lookup fail loudly, and record what asked for it."""
    time_zones.clear()
    asked: list[str] = []

    def refuse(name: str) -> Any:
        asked.append(name)
        raise BlockingLookup(f"blocking time-zone lookup of {name!r}")

    monkeypatch.setattr(dt_util, "get_time_zone", refuse)
    return asked


def test_every_zone_key_in_a_document_is_found() -> None:
    document = {
        "v": 2,
        "areas": [
            {"id": "GB-A", "tz": "Europe/London", "market_tz": "Europe/Paris",
             "publication": {"time": "16:00", "tz": "Europe/London"}},
            {"id": "PT", "tz": "Europe/Lisbon", "market_tz": "Europe/Madrid"},
            {"id": "X", "tz": 7},
        ],
    }
    assert time_zones.zone_names(document) == {"Europe/London", "Europe/Paris", "Europe/Lisbon", "Europe/Madrid"}


def test_the_fixed_zones_include_the_default_publication_clock() -> None:
    assert DEFAULT_PUBLICATION.tz in time_zones.DEFAULT_ZONES
    assert PUBLICATION_ZONE in time_zones.DEFAULT_ZONES


async def test_the_bundled_catalogue_and_a_fetched_day_are_resolved_before_parsing(
    hass: HomeAssistant, no_blocking_lookup: list[str]
) -> None:
    transport = StubTransport()
    transport.serve_area()
    # The list is read as setup reads it, which resolves every zone it names off the loop.
    repository = stub_repository(hass, transport, now=Clock(), bundled=await async_load_bundled(hass))

    catalogue = repository.catalogue_snapshot()
    day = await repository.async_get_day("SE4", TODAY)

    assert catalogue.state == "ready" and catalogue.catalogue is not None
    assert day.state == "ready"
    for name in (*ZONES, *time_zones.DEFAULT_ZONES):
        assert time_zones.resolved(name), name
    # What the rest of the integration computes on the loop reads the cache.
    now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    assert local_dates(now, "Europe/Stockholm") == (TODAY, TODAY + timedelta(days=1))
    start, end = display_day_span(TODAY, "Europe/Berlin")
    assert end - start == timedelta(hours=24)
    assert market_day_of(now, "Europe/Oslo") == TODAY
    assert in_publication_window(datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)) is True
    assert no_blocking_lookup == []


async def test_a_restored_cache_is_resolved_before_it_is_parsed(
    hass: HomeAssistant, no_blocking_lookup: list[str]
) -> None:
    transport = StubTransport()
    transport.serve_area()
    store = StoreDouble()
    first = stub_repository(hass, transport, store=store, now=Clock(), bundled=await async_load_bundled(hass))
    first.catalogue_snapshot()
    await first.async_get_day("SE4", TODAY)
    await hass.async_block_till_done()
    assert store.payload is not None

    # A new process: nothing resolved, nothing fetched.
    time_zones.clear()
    idle = StubTransport()
    restarted = stub_repository(
        hass, idle, store=StoreDouble(store.payload), now=Clock(), bundled=await async_load_bundled(hass)
    )
    await restarted.async_restore()

    assert restarted.catalogue_snapshot().catalogue is not None
    assert restarted.day_snapshot("SE4", TODAY).state == "ready"
    for name in ZONES:
        assert time_zones.resolved(name), name
    assert idle.calls == [] and no_blocking_lookup == []


async def test_an_unresolved_zone_on_the_loop_is_never_looked_up_there(
    hass: HomeAssistant, no_blocking_lookup: list[str]
) -> None:
    assert time_zones.zone("Asia/Tokyo") is None
    with pytest.raises(time_zones.ZoneNotResolved):
        time_zones.require_zone("Asia/Tokyo")
    assert no_blocking_lookup == []

    # Its resolution was scheduled off the loop; once it ran, the name reads from the cache.
    await asyncio.gather(*list(time_zones._TASKS))
    tokyo = time_zones.zone("Asia/Tokyo")
    assert tokyo is not None and tokyo.utcoffset(datetime(2026, 1, 1)) == timedelta(hours=9)
    assert no_blocking_lookup == []


async def test_a_name_that_is_not_a_zone_is_cached_as_none(hass: HomeAssistant, no_blocking_lookup: list[str]) -> None:
    await time_zones.async_resolve(("Mars/Olympus", "../etc/passwd", "x" * 200))
    assert time_zones.zone("Mars/Olympus") is None
    assert time_zones.zone("../etc/passwd") is None
    assert no_blocking_lookup == []


async def test_home_assistants_own_zone_and_utc_need_no_lookup(
    hass: HomeAssistant, no_blocking_lookup: list[str]
) -> None:
    assert time_zones.zone(hass.config.time_zone) is dt_util.get_default_time_zone()
    assert time_zones.zone("UTC") is dt_util.UTC
    assert no_blocking_lookup == []


def test_off_the_loop_an_unresolved_name_is_looked_up_on_the_spot() -> None:
    zone = time_zones.zone("America/New_York")
    assert zone is not None and time_zones.resolved("America/New_York")
