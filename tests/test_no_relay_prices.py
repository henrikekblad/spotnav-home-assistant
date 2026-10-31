"""2.0: no price ever comes from the SpotNav relay, and an upgraded installation keeps what it holds.

* No request for prices goes to the relay's host, from any path that reads prices: setup, the refresh cycle, a
  manual refresh, the archive read of the history import, the history backfill. The relay's address is left only
  in the push module, which is the one thing the relay still does.
* A store written before 2.0 (schema 1: the relay's area list, index and profiles beside `AREA|date` days) is read
  with every day it held; the relay's resources are dropped on the next write, and the days stay labelled
  `"provider": "relay"`.
* On the first start of 2.0, an area with no source of its own (no Nord Pool entry, no PVPC or Octopus integration,
  no ENTSO-E key) shows the notice that names what to add, keeps showing the prices it already holds, and raises
  the Repairs issue only once there is nothing priced for today.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.notifications import push as push_module
from custom_components.spotnav.pricing.price_repository import STORAGE_KEY
from custom_components.spotnav.runtime import domain_data

from .relay import day_body, fixture, SE4, TODAY, YESTERDAY
from .test_price_device import add_price, NOW

#: The relay's host: since 2.0 it is asked for push wake-ups only, never for prices.
RELAY_HOST = urlsplit(push_module.DEFAULT_BASE_URL).hostname
PACKAGE = Path(__file__).resolve().parent.parent / "custom_components" / "spotnav"


def relay_era_store() -> dict[str, Any]:
    """The store as 1.x wrote it (schema 1): the relay's list, index and profile, and SE4's days."""

    def stored(document: Any) -> dict[str, Any]:
        return {"document": document, "fetched_at": "2026-09-21T12:00:00+00:00"}

    return {
        "schema": 1,
        "catalogue": stored(json.loads(fixture("areas.json"))),
        "catalogue_rev": "916f2f8e05fd",
        "index": stored(json.loads(fixture("index.json"))),
        "days": {
            f"SE4|{YESTERDAY.isoformat()}": stored(json.loads(day_body(SE4, YESTERDAY))),
            f"SE4|{TODAY.isoformat()}": stored(json.loads(day_body(SE4, TODAY))),
        },
        "profiles": {"SE4": stored(json.loads(fixture("profile_SE4.json")))},
    }


def issues(hass: HomeAssistant) -> list[str]:
    return [issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == DOMAIN]


# ------------------------------------------------------------------ no request to the relay for prices


def test_the_relay_address_is_left_in_the_push_module_only() -> None:
    """The source names the relay's host only where push uses it; nothing under `pricing/` does."""
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "push.py":
            assert "DEFAULT_BASE_URL" in text
            continue
        assert "DEFAULT_BASE_URL" not in text, path
        if path.parent.name in ("pricing", "providers"):
            assert RELAY_HOST not in text, path
    assert not (PACKAGE / "pricing" / "providers" / "relay.py").exists()


async def test_no_price_path_asks_the_relay(hass: HomeAssistant, aioclient_mock, hass_storage: dict[str, Any]) -> None:
    """Every path that reads prices, on an installation that still holds the relay's days: not one request to the
    relay's host (Home Assistant's own client session is the one every provider uses)."""
    hass_storage[STORAGE_KEY] = {"version": 1, "key": STORAGE_KEY, "data": relay_era_store()}
    with freeze_time(NOW):
        await add_price(hass)
        repository = domain_data(hass).price_repository
        manager = domain_data(hass).price_refresh
        await manager.async_refresh_area(SE4)
        await repository.async_get_day(SE4, TODAY + timedelta(days=1), refresh=True)
        assert await repository.async_get_archive_day(SE4, TODAY - timedelta(days=30)) is None
        history = await repository.async_fetch_history(SE4, [TODAY - timedelta(days=days) for days in range(2, 9)])
        assert history.failed == "source_missing" and history.stored == ()
        assert await repository.async_get_profile(SE4) is None
        await hass.async_block_till_done()

    asked = [str(url) for _method, url, *_ in aioclient_mock.mock_calls]
    assert not [url for url in asked if urlsplit(url).hostname == RELAY_HOST], asked


# ------------------------------------------------------------------ an upgraded store


async def test_an_upgraded_store_keeps_every_held_day_and_drops_the_relays_resources(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    hass_storage[STORAGE_KEY] = {"version": 1, "key": STORAGE_KEY, "data": relay_era_store()}
    with freeze_time(NOW):
        price = await add_price(hass)
        repository = domain_data(hass).price_repository
        await domain_data(hass).price_refresh.async_refresh_area(SE4)
        await hass.async_block_till_done()

        # The held days are read as they were: today's prices show, from the store.
        for day in (YESTERDAY, TODAY):
            snapshot = repository.day_snapshot(SE4, day)
            assert snapshot.document is not None and snapshot.source == "store", day
        assert price.runtime_data.device.points, "today's held prices are the device's prices"

        await repository.async_persist_history()
    written = hass_storage[STORAGE_KEY]["data"]
    assert written["schema"] == 2
    # Each day under its own calendar, labelled with the source it came from; nothing else of the relay's.
    assert set(written["days"]) == {"SE4|Europe/Stockholm|2026-09-21", "SE4|Europe/Stockholm|2026-09-22"}
    assert {entry["provider"] for entry in written["days"].values()} == {"relay"}
    assert set(written) == {"schema", "days"}


# ------------------------------------------------------------------ the first start of 2.0


@pytest.mark.parametrize(
    ("area", "notice"),
    [
        (SE4, "add_nordpool"),
        ("FI", "add_nordpool"),
        ("ES", "add_entsoe_key"),
        ("ES-PVPC", "add_pvpc"),
        ("GB-C", "add_octopus"),
    ],
)
async def test_an_area_without_a_source_of_its_own_names_what_to_add(
    hass: HomeAssistant, area: str, notice: str
) -> None:
    off = {"enabled": False, "value": None}
    with freeze_time(NOW):
        price = await add_price(hass, area=area, vat=off, tax=off, transfer=off)
        await domain_data(hass).price_refresh.async_refresh_area(area)
        await hass.async_block_till_done()
    device = price.runtime_data.device
    assert device.source is None and device.problem == notice
    status = domain_data(hass).price_repository.source_status(area)
    assert status.key is None and status.notice is not None and status.notice.code == notice


async def test_with_nord_pool_added_the_held_days_stay_and_the_notice_goes(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    hass_storage[STORAGE_KEY] = {"version": 1, "key": STORAGE_KEY, "data": relay_era_store()}
    nordpool = MockConfigEntry(domain="nordpool", title="Nord Pool", data={})
    nordpool.add_to_hass(hass)
    try:
        with freeze_time(NOW):
            price = await add_price(hass)
            nordpool.mock_state(hass, ConfigEntryState.LOADED)
            await hass.async_block_till_done()
            device = price.runtime_data.device
            assert device.source == "nordpool" and device.problem is None
            # Today's file from before 2.0 is on the same calendar as Nord Pool's (a Stockholm day is a CET day):
            # it stays the day's prices, and nothing asks for it again.
            assert domain_data(hass).price_repository.day_snapshot(SE4, TODAY).document is not None
    finally:
        nordpool.mock_state(hass, ConfigEntryState.NOT_LOADED)


async def test_the_repair_waits_until_nothing_is_priced_for_today(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    hass_storage[STORAGE_KEY] = {"version": 1, "key": STORAGE_KEY, "data": relay_era_store()}
    with freeze_time(NOW):
        price = await add_price(hass)
        manager = domain_data(hass).price_refresh
        await manager.async_refresh_area(SE4)
        await hass.async_block_till_done()
        # The notice at once; no Repairs issue while today's held prices last.
        assert price.runtime_data.device.problem == "add_nordpool"
        assert issues(hass) == []

    # The next day: nothing held for it, nothing asked of anyone, and now the issue stands.
    with freeze_time(NOW + timedelta(days=1)):
        await manager.async_refresh_area(SE4)
        await hass.async_block_till_done()
        snapshot = manager.area_snapshot(f"{SE4}")
        assert snapshot is not None and snapshot.today_document is None
        assert snapshot.today_snapshot.attempt_error == "source_missing"
        assert issues(hass) == [f"price_source_missing_{price.entry_id}"]
