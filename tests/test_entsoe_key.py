"""The installation's ENTSO-E key: the WebSocket command, the electricity price's Configure, diagnostics, and a scan
that runs every failure path with a sentinel token and finds it nowhere but in the key's own stored field.

Real config entries on the offline relay; the Transparency Platform is the fake behind Home Assistant's session
(`test_price_provider_entsoe.FakePlatform`), never the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from zoneinfo import ZoneInfo
from datetime import date
from typing import Any

import pytest
from aiohttp import ClientError, ClientResponseError, RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.debug_bundle import async_build_debug_bundle
from custom_components.spotnav.diagnostics import entry_diagnostics
from custom_components.spotnav.pricing.entsoe_parse import EntsoeToken, market_day_bounds
from custom_components.spotnav.pricing.providers import entsoe as entsoe_module
from custom_components.spotnav.runtime import domain_data

from .entsoe_helpers import fixture, SECRET
from .test_price_device import add_price, NOW
from .test_price_provider_entsoe import FakePlatform
from .world import admin, non_admin, setup_charger, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

TODAY = date(2026, 9, 22)
STORE_KEY = "spotnav.price_sources"


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    fake = FakePlatform()
    monkeypatch.setattr(entsoe_module, "async_get_clientsession", lambda _hass: fake)
    monkeypatch.setattr(entsoe_module, "MIN_SPACING_S", 0.0)
    return fake


def store(hass: HomeAssistant):
    return domain_data(hass).price_store


async def setup(hass: HomeAssistant) -> None:
    """The integration without any entry: what answers the command."""
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()


async def set_key(client: Any, key: str | None, **extra: Any) -> dict[str, Any]:
    return await ws_call(client, {"type": "spotnav/set_entsoe_key", "key": key, **extra})


# ------------------------------------------------------------------ the WebSocket command


async def test_an_accepted_key_is_stored_and_used(hass: HomeAssistant, hass_ws_client, platform) -> None:
    await add_price(hass)
    today = dt_util.utcnow().astimezone(ZoneInfo("Europe/Stockholm")).date()
    platform.serve_day("SE4", today)
    client = await admin(hass, hass_ws_client)

    frame = await set_key(client, f"  {SECRET}\n")

    assert frame["success"] and frame["result"] == {"ok": True, "error": None, "state": "ok"}
    # One check, for the price entry's area and today's CET day.
    assert platform.requests[0]["in_Domain"] == "10Y1001A1001A47J"
    assert platform.requests[0]["periodStart"] == market_day_bounds(today, "Europe/Stockholm")[0].strftime("%Y%m%d%H%M")
    assert store(hass).entsoe_key_state == "ok" and store(hass).entsoe_key() == EntsoeToken(SECRET)
    assert domain_data(hass).price_repository.provider_for("ES").key == "entsoe"


async def test_a_rejected_key_is_not_stored(hass: HomeAssistant, hass_ws_client, platform) -> None:
    platform.fallback = (401, "Unauthorized", {})
    await setup(hass)
    client = await admin(hass, hass_ws_client)

    frame = await set_key(client, SECRET)

    assert frame["result"] == {"ok": False, "error": "entsoe_key_rejected", "state": None}
    assert not store(hass).entsoe_key_set
    # Without an electricity price, the key is checked against DE-LU.
    assert platform.requests[0]["in_Domain"] == "10Y1001A1001A82H"


async def test_a_key_that_cannot_be_checked_is_stored_unchecked(hass: HomeAssistant, hass_ws_client, platform) -> None:
    platform.fallback = (503, "", {})
    await setup(hass)
    client = await admin(hass, hass_ws_client)

    frame = await set_key(client, SECRET)

    assert frame["result"] == {"ok": False, "error": "entsoe_unreachable", "state": "unchecked"}
    assert store(hass).entsoe_key_state == "unchecked" and store(hass).entsoe_key_usable


async def test_a_rejected_replacement_keeps_the_stored_key(hass: HomeAssistant, hass_ws_client, platform) -> None:
    await setup(hass)
    client = await admin(hass, hass_ws_client)
    await set_key(client, SECRET)
    platform.fallback = (403, "", {})

    frame = await set_key(client, "another-key")

    assert frame["result"] == {"ok": False, "error": "entsoe_key_rejected", "state": "ok"}
    assert store(hass).entsoe_key() == EntsoeToken(SECRET)


async def test_clearing_needs_no_request(hass: HomeAssistant, hass_ws_client, platform) -> None:
    await setup(hass)
    client = await admin(hass, hass_ws_client)
    await set_key(client, SECRET)
    asked = len(platform.requests)

    assert (await set_key(client, None))["result"] == {"ok": True, "error": None, "state": None}
    assert (await set_key(client, "   "))["result"] == {"ok": True, "error": None, "state": None}
    assert len(platform.requests) == asked and not store(hass).entsoe_key_set


async def test_only_an_administrator_sets_the_key(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, platform
) -> None:
    await setup(hass)
    client = await non_admin(hass, hass_ws_client, hass_read_only_access_token)

    frame = await set_key(client, SECRET)

    assert frame["success"] is False and frame["error"]["code"] == "spotnav_not_admin"
    assert platform.requests == [] and not store(hass).entsoe_key_set


# ------------------------------------------------------------------ the electricity price's Configure


async def test_configure_offers_the_key_and_stores_an_accepted_one(hass: HomeAssistant, platform, freezer) -> None:
    freezer.move_to(NOW)
    entry = await add_price(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    fields = [str(key) for key in result["data_schema"].schema]
    assert fields == ["area_id", "entsoe_key"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"area_id": "SE4", "entsoe_key": SECRET}
    )

    assert result["step_id"] == "price_fees"
    assert store(hass).entsoe_key_state == "ok"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"vat_mode": "suggested", "tax_mode": "suggested", "transfer_mode": "off"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The key is the installation's, not the entry's.
    assert SECRET not in json.dumps(dict(entry.data)) and SECRET not in json.dumps(dict(entry.options))


async def test_configure_keeps_a_rejected_key_out_and_the_form_open(hass: HomeAssistant, platform, freezer) -> None:
    freezer.move_to(NOW)
    entry = await add_price(hass)
    platform.fallback = (401, "", {})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"area_id": "SE4", "entsoe_key": SECRET}
    )

    assert result["type"] is FlowResultType.FORM and result["step_id"] == "price_init"
    assert result["errors"] == {"entsoe_key": "entsoe_key_rejected"}
    assert not store(hass).entsoe_key_set
    # Never shown back.
    assert SECRET not in str(result)


async def test_configure_leaves_the_key_alone_when_empty_and_removes_it_on_request(
    hass: HomeAssistant, platform, freezer
) -> None:
    freezer.move_to(NOW)
    entry = await add_price(hass)
    await store(hass).async_set_entsoe_key(EntsoeToken(SECRET), "ok")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert [str(key) for key in result["data_schema"].schema] == ["area_id", "entsoe_key", "entsoe_key_remove"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"area_id": "SE4"})
    assert result["step_id"] == "price_fees" and store(hass).entsoe_key_state == "ok"
    assert platform.requests == []

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"area_id": "SE4", "entsoe_key_remove": True}
    )
    assert result["step_id"] == "price_fees" and not store(hass).entsoe_key_set


# ------------------------------------------------------------------ diagnostics


async def test_diagnostics_say_set_or_unset(hass: HomeAssistant, platform, freezer) -> None:
    freezer.move_to(NOW)
    entry = await add_price(hass)
    assert entry_diagnostics(hass, entry)["price_data"]["entsoe_key"] == "unset"

    await store(hass).async_set_entsoe_key(EntsoeToken(SECRET), "unchecked")

    price_data = entry_diagnostics(hass, entry)["price_data"]
    assert price_data["entsoe_key"] == "set" and price_data["entsoe_key_state"] == "unchecked"


# ------------------------------------------------------------------ the token scan


def _url_error(kind: type[BaseException]):
    """A transport failure whose text, and whose cause's, holds the whole request URL, as aiohttp's do."""

    def raise_it(url: str) -> BaseException:
        cause = OSError(f"connect failed for {url}")
        error = kind(f"Cannot connect to host for {url}")
        error.__cause__ = cause
        error.__context__ = ValueError(url)
        return error

    return raise_it


def _response_error(url: str) -> BaseException:
    info = RequestInfo(URL(url), "GET", CIMultiDictProxy(CIMultiDict()), URL(url))
    return ClientResponseError(info, (), status=500, message=f"bad gateway at {url}")


FAILURES: list[Any] = [
    (401, "Unauthorized", {}),
    (403, "", {}),
    (429, "", {"Retry-After": "30"}),
    (500, f"echo {SECRET}", {}),
    (400, f"<html>{SECRET}</html>", {}),
    (200, fixture("synthetic_ack_999_auth.xml"), {}),
    (200, fixture("synthetic_ack_999_unknown.xml"), {}),
    (200, f"<Publication_MarketDocument><type>{SECRET}</type></Publication_MarketDocument>", {}),
    (200, b"x" * (4 * 1024 * 1024 + 10), {}),
    _url_error(ClientError),
    _url_error(TimeoutError),
    _url_error(RuntimeError),
    _response_error,
]


def _texts(err: BaseException) -> list[str]:
    seen: list[BaseException] = []
    current: BaseException | None = err
    while current is not None and all(current is not item for item in seen):
        seen.append(current)
        current = current.__cause__ or current.__context__
    return [str(item) + repr(item) for item in seen] + ["".join(traceback.format_exception(err))]


async def test_the_token_is_nowhere_after_any_failure(
    hass: HomeAssistant, hass_ws_client, hass_storage, platform, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    charger = await setup_charger(hass)
    entry = await add_price(hass)
    client = await admin(hass, hass_ws_client)
    keys = store(hass)
    repository = domain_data(hass).price_repository
    provider = repository.entsoe_provider()
    answers: list[Any] = []
    raised: list[BaseException] = []

    for failure in FAILURES:
        platform.fallback = failure
        # The WebSocket check, the options flow's check, and a fetch during operation, each with the sentinel.
        answers.append(await set_key(client, SECRET))
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        answers.append(
            await hass.config_entries.options.async_configure(flow["flow_id"], {"area_id": "SE4", "entsoe_key": SECRET})
        )
        await keys.async_set_entsoe_key(EntsoeToken(SECRET), "unchecked")
        for area_id in ("SE4", "ES", "FI"):
            try:
                answers.append(await provider.async_fetch_day(area_id, TODAY))
                answers.append(await repository.async_get_file(area_id, TODAY, refresh=True))
                answers.append(await repository.async_get_archive_day(area_id, date(2025, 1, 1)))
            except Exception as err:  # noqa: BLE001 - none is expected; any that is must not carry the token
                raised.append(err)
        # Out of any wait the platform asked for, for the next failure.
        provider._quiet_until = None
        await hass.async_block_till_done()

    # A request cancelled while it is out.
    gate = asyncio.Event()

    async def hang(url: str, headers: dict[str, str]):
        await gate.wait()

    platform.answer = hang  # type: ignore[method-assign]
    await keys.async_set_entsoe_key(EntsoeToken(SECRET), "ok")
    task = hass.async_create_task(provider.async_fetch_day("SE4", date(2026, 9, 20)))
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError as err:
        raised.append(err)
    # Anything else that reached the platform meanwhile (a cycle after the key was set) gets its answer.
    gate.set()
    await hass.async_block_till_done()

    assert platform.requests and all(query["securityToken"] == SECRET for query in platform.requests)
    for err in raised:
        for text in _texts(err):
            assert SECRET not in text
    assert SECRET not in repr(answers)
    for record in caplog.records:
        # Home Assistant's own WebSocket debug log repeats every received message, as for any command; that is the
        # transport's, not SpotNav's.
        if record.name.startswith("homeassistant.components.websocket_api"):
            continue
        # The test harness's stand-in for Home Assistant's storage logs every write; the real store does not.
        if record.name == "pytest_homeassistant_custom_component.common":
            continue
        assert SECRET not in record.getMessage(), record.name
        if record.exc_info:
            assert SECRET not in "".join(traceback.format_exception(*record.exc_info)), record.name
    # Diagnostics, the debug bundle and the dashboard say only whether a key is set.
    texts = [
        json.dumps(entry_diagnostics(hass, entry), default=str),
        json.dumps(entry_diagnostics(hass, charger), default=str),
        json.dumps(await async_build_debug_bundle(hass), default=str),
        json.dumps(
            dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, charger), can_act=True),
            default=str,
        ),
    ]
    for text in texts:
        assert SECRET not in text
    # Stored: only in the key's own field.
    await keys.async_save()
    for key, stored in hass_storage.items():
        data = dict(stored.get("data") or {})
        if key == STORE_KEY:
            assert data.pop("entsoe_token") == SECRET
        assert SECRET not in json.dumps(data, default=str), key
