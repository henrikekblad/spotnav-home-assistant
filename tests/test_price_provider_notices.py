"""An area's price source and notice: the table, the router that reads it from Home Assistant, the dashboard's
`prices.provider` and `market.source`, the app's `reads` gating, the key from the card's WebSocket command, and the
electricity price device's problem, `source` attribute and Repairs issue.

Real config entries with the production router: an area gets prices only from a provider the person added (no
SpotNav relay since 2.0); the Transparency Platform is the fake behind Home Assistant's session, never the network.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.webhook import _for_app
from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.pricing.area_catalogue import async_load_bundled
from custom_components.spotnav.pricing.entsoe_parse import EntsoeToken
from custom_components.spotnav.pricing.price_repository import PriceRepository
from custom_components.spotnav.pricing.price_store import PriceSourceStore
from custom_components.spotnav.pricing.providers import ProviderRouter
from custom_components.spotnav.pricing.providers import entsoe as entsoe_module
from custom_components.spotnav.pricing.providers.entsoe import EntsoeProvider
from custom_components.spotnav.pricing.providers.nordpool import NordPoolProvider
from custom_components.spotnav.pricing.providers.notices import (
    NOTICE_CODES,
    ProviderNotice,
    source_notice,
    SourceFacts,
)
from custom_components.spotnav.pricing.providers.pvpc import PvpcProvider
from custom_components.spotnav.runtime import domain_data

from .entsoe_helpers import SECRET
from .relay import Clock, day_body, StoreDouble, StubTransport, TODAY
from .test_price_device import add_price, entity, NOW
from .test_price_provider_entsoe import FakePlatform
from .test_webhook_parity import post
from .world import admin, setup_charger, ws_call

NP = {"nordpool_route": True, "entsoe_route": True}
ENTSOE = {"entsoe_route": True}
PVPC = {"pvpc_route": True}
GB = {"octopus_route": True}


# ------------------------------------------------------------------ the table


@pytest.mark.parametrize(
    ("facts", "code"),
    [
        (SourceFacts(**PVPC, pvpc_entry="loaded"), None),
        (SourceFacts(**PVPC, pvpc_entry="not_loaded"), "pvpc_not_loaded"),
        (SourceFacts(**PVPC), "add_pvpc"),
        (SourceFacts(**GB, octopus_entry="loaded", octopus_meter=True), None),
        (SourceFacts(**GB, octopus_entry="loaded", octopus_meter=True, octopus_region="A"), "octopus_region_mismatch"),
        (SourceFacts(**GB, octopus_entry="not_loaded"), "octopus_not_loaded"),
        (SourceFacts(**GB, octopus_entry="loaded"), "octopus_no_agile"),
        (SourceFacts(**GB), "add_octopus"),
        (SourceFacts(**NP, nordpool_entry="loaded"), None),
        (SourceFacts(**NP, entsoe_key="ok"), None),
        (SourceFacts(**NP, entsoe_key="unchecked"), None),
        (SourceFacts(**NP, nordpool_entry="not_loaded", entsoe_key="ok"), None),
        (SourceFacts(**NP, nordpool_entry="not_loaded"), "nordpool_not_loaded"),
        (SourceFacts(**NP, nordpool_entry="not_loaded", entsoe_key="rejected"), "nordpool_not_loaded"),
        (SourceFacts(**NP, entsoe_key="rejected"), "entsoe_key_rejected"),
        (SourceFacts(**NP), "add_nordpool"),
        (SourceFacts(**ENTSOE, entsoe_key="ok"), None),
        (SourceFacts(**ENTSOE, entsoe_key="unchecked"), None),
        (SourceFacts(**ENTSOE, entsoe_key="rejected"), "entsoe_key_rejected"),
        (SourceFacts(**ENTSOE), "add_entsoe_key"),
        # A Nord Pool entry is no source for an area Nord Pool does not trade.
        (SourceFacts(**ENTSOE, nordpool_entry="loaded"), "add_entsoe_key"),
        (SourceFacts(), "source_missing"),
        (SourceFacts(nordpool_entry="loaded", entsoe_key="ok"), "source_missing"),
    ],
)
def test_the_notice_table(facts: SourceFacts, code: str | None) -> None:
    notice = source_notice(facts)
    assert (None if notice is None else notice.code) == code
    assert code is None or code in NOTICE_CODES


def test_the_region_mismatch_names_the_meters_region() -> None:
    notice = source_notice(SourceFacts(**GB, octopus_entry="loaded", octopus_meter=True, octopus_region="A"))
    assert notice is not None and notice.as_dict() == {"code": "octopus_region_mismatch", "params": {"region": "A"}}
    assert ProviderNotice("add_nordpool").as_dict() == {"code": "add_nordpool", "params": {}}


# ------------------------------------------------------------------ the router


class World:
    def __init__(self, hass: HomeAssistant, transport: StubTransport) -> None:
        self.hass = hass
        self.transport = transport
        self.clock = Clock(NOW)
        self.changes = 0

    async def start(self) -> None:
        self.keys = PriceSourceStore(self.hass)
        await self.keys.async_load()
        bundled = await async_load_bundled(self.hass)
        assert bundled is not None
        self.router = ProviderRouter(
            NordPoolProvider(self.hass, bundled, None, now=self.clock),
            EntsoeProvider(self.hass, bundled, self.keys, None, session=FakePlatform(), now=self.clock),
            pvpc=PvpcProvider(self.hass, bundled, now=self.clock),
        )
        self.router.async_start(self.hass)
        self.router.add_listener(self._changed)
        self.repository = PriceRepository(
            self.hass, now=self.clock, store=StoreDouble(), bundled=bundled, router=self.router
        )

    def _changed(self) -> None:
        self.changes += 1

    def status(self, area_id: str) -> tuple[str | None, str | None]:
        status = self.repository.source_status(area_id)
        return status.key, None if status.notice is None else status.notice.code


@pytest.fixture
async def world(hass: HomeAssistant, transport: StubTransport):
    instance = World(hass, transport)
    yield instance
    if hasattr(instance, "router"):
        instance.router.async_stop()
    for domain in ("nordpool", "pvpc_hourly_pricing"):
        for entry in hass.config_entries.async_entries(domain):
            entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


async def test_a_nord_pool_area_has_no_source_and_asks_for_nord_pool(world: World) -> None:
    await world.start()
    assert world.status("SE4") == (None, "add_nordpool")
    status = world.repository.source_status("SE4")
    assert (status.name, status.url) == (None, None)


async def test_an_area_without_its_integration_or_a_key_has_no_source(world: World) -> None:
    await world.start()
    # FI is a Nord Pool area; ES is ENTSO-E only; XX is nobody's.
    assert world.status("FI") == (None, "add_nordpool")
    assert world.status("ES") == (None, "add_entsoe_key")
    assert world.status("ES-PVPC") == (None, "add_pvpc")
    assert world.status("XX") == (None, "source_missing")
    status = world.repository.source_status("ES")
    assert (status.name, status.url) == (None, None)


async def test_a_nordpool_entry_that_did_not_start_says_so_and_is_followed(world: World) -> None:
    await world.start()
    entry = MockConfigEntry(domain="nordpool", title="Nord Pool", data={})
    entry.add_to_hass(world.hass)
    entry.mock_state(world.hass, ConfigEntryState.SETUP_ERROR)
    # Still no provider, but the notice moved: the listeners were told.
    assert world.changes == 1
    assert world.status("SE4") == (None, "nordpool_not_loaded")

    entry.mock_state(world.hass, ConfigEntryState.LOADED)
    assert world.changes == 2
    assert world.status("SE4") == ("nordpool", None)
    nordpool = world.repository.source_status("SE4")
    assert (nordpool.name, nordpool.url) == ("Nord Pool", "https://www.nordpoolgroup.com/")
    # Nord Pool does not trade Spain.
    assert world.status("ES") == (None, "add_entsoe_key")


async def test_the_key_moves_the_notice_and_the_provider(world: World) -> None:
    await world.start()
    await world.keys.async_set_entsoe_key(EntsoeToken(SECRET), "rejected")
    assert world.status("ES") == (None, "entsoe_key_rejected")
    assert world.status("SE4") == (None, "entsoe_key_rejected")

    await world.keys.async_set_entsoe_key(EntsoeToken(SECRET), "ok")
    assert world.status("ES") == ("entsoe", None)
    assert world.status("SE4") == ("entsoe", None)

    await world.keys.async_set_entsoe_key(None)
    assert world.status("ES") == (None, "add_entsoe_key")


async def test_a_pvpc_entry_that_did_not_start_says_so(world: World) -> None:
    await world.start()
    entry = MockConfigEntry(domain="pvpc_hourly_pricing", title="PVPC", unique_id="2.0TD", data={"tariff": "2.0TD"})
    entry.add_to_hass(world.hass)
    entry.mock_state(world.hass, ConfigEntryState.SETUP_RETRY)
    assert world.status("ES-PVPC") == (None, "pvpc_not_loaded")
    entry.mock_state(world.hass, ConfigEntryState.LOADED)
    assert world.status("ES-PVPC") == ("pvpc", None)


# ------------------------------------------------------------------ the dashboard and the app


def dashboard(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    entry = hass.config_entries.async_get_entry(entry_id)
    assert entry is not None
    return dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, entry), can_act=True)


async def test_the_dashboard_names_the_provider_and_its_attribution(hass: HomeAssistant) -> None:
    with freeze_time(NOW):
        await setup_charger(hass, entry_id="entry_np", webhook_id="webhook-np")
        await add_price(hass)
        before = dashboard(hass, "entry_np")
        assert before["prices"]["provider"] == {
            "key": None,
            "name": None,
            "url": None,
            "notice": {"code": "add_nordpool", "params": {}},
        }
        assert before["market"]["source"] is None
        entry = MockConfigEntry(domain="nordpool", title="Nord Pool", data={})
        entry.add_to_hass(hass)
        entry.mock_state(hass, ConfigEntryState.LOADED)
        try:
            after = dashboard(hass, "entry_np")
        finally:
            entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    assert after["prices"]["provider"] == {
        "key": "nordpool",
        "name": "Nord Pool",
        "url": "https://www.nordpoolgroup.com/",
        "notice": None,
    }
    assert after["market"]["source"] == {"name": "Nord Pool", "url": "https://www.nordpoolgroup.com/"}


def test_the_app_gets_prices_provider_only_when_it_reads_it() -> None:
    body = {"prices": {"state": "ready", "provider": {"key": "nordpool"}}, "market": {"source": None}}
    assert _for_app(body, {}) == {"prices": {"state": "ready"}, "market": {"source": None}}
    assert _for_app(body, {"reads": ["price_source"]})["prices"] == {"state": "ready"}
    assert _for_app(body, {"reads": ["price_provider"]}) == body
    # Nothing to take out when the block is not there.
    assert _for_app({"prices": None}, {}) == {"prices": None}


async def test_the_webhook_dashboard_carries_the_provider_for_an_app_that_reads_it(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    with freeze_time(NOW):
        await setup_charger(hass, entry_id="entry_app", webhook_id="webhook-app")
        await add_price(hass)
        client = await hass_client_no_auth()
        _, plain = await post(client, "webhook-app", {"action": "dashboard", "api_version": 1})
        _, reading = await post(
            client, "webhook-app", {"action": "dashboard", "api_version": 1, "reads": ["price_provider"]}
        )
    assert "provider" not in plain["prices"]
    assert reading["prices"]["provider"]["notice"] == {"code": "add_nordpool", "params": {}}


# ------------------------------------------------------------------ the card's key, the device and Repairs


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    fake = FakePlatform()
    monkeypatch.setattr(entsoe_module, "async_get_clientsession", lambda _hass: fake)
    monkeypatch.setattr(entsoe_module, "MIN_SPACING_S", 0.0)
    return fake


def _issues(hass: HomeAssistant) -> list[str]:
    return [issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == DOMAIN]


async def test_the_cards_key_gives_an_entsoe_area_its_source_and_ends_the_repair(
    hass: HomeAssistant, hass_ws_client, platform: FakePlatform
) -> None:
    """Spain with no key: no source at all, so once a price cycle found nothing for today the electricity price raises
    its Repairs issue. The key from the card's command gives the area ENTSO-E, the prices come, and the issue goes by
    itself."""
    await setup_charger(hass, entry_id="entry_es", webhook_id="webhook-es")
    off = {"enabled": False, "value": None}
    price = await add_price(hass, area="ES", vat=off, tax=off, transfer=off)
    manager = domain_data(hass).price_refresh
    await manager.async_refresh_area("ES")
    await hass.async_block_till_done()

    device = price.runtime_data.device
    assert device.problem == "add_entsoe_key" and device.source is None
    assert dashboard(hass, "entry_es")["prices"]["provider"] == {
        "key": None,
        "name": None,
        "url": None,
        "notice": {"code": "add_entsoe_key", "params": {}},
    }
    assert _issues(hass) == [f"price_source_missing_{price.entry_id}"]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"price_source_missing_{price.entry_id}")
    assert issue is not None and issue.translation_placeholders == {"price": "Electricity price ES", "area": "ES"}

    today = dt_util.utcnow().astimezone(ZoneInfo("Europe/Madrid")).date()
    for day in (today, today + date.resolution):
        platform.serve_day("ES", day)
    client = await admin(hass, hass_ws_client)
    frame = await ws_call(client, {"type": "spotnav/set_entsoe_key", "key": SECRET, "area_id": "ES"})
    assert frame["result"] == {"ok": True, "error": None, "state": "ok"}
    await manager.async_refresh_area("ES")
    await hass.async_block_till_done()

    provider = dashboard(hass, "entry_es")["prices"]["provider"]
    assert provider["key"] == "entsoe" and provider["notice"] is None
    assert device.source == "entsoe" and device.problem is None
    assert _issues(hass) == []
    state = hass.states.get(entity(hass, price.entry_id, "price_now", "sensor"))
    assert state is not None and state.attributes["source"] == "entsoe"


async def test_an_area_still_holding_todays_prices_has_a_notice_and_no_repair(hass: HomeAssistant) -> None:
    """An upgraded installation without Nord Pool: today's file kept from before 2.0 still shows, the notice asks for
    Nord Pool at once, and the Repairs issue waits until there are no prices for today."""
    with freeze_time(NOW):
        price = await add_price(hass)
        repository = domain_data(hass).price_repository
        repository._adopt(  # noqa: SLF001 - the store's own path, as an upgraded store restores
            {"schema": 2, "days": {"SE4|Europe/Stockholm|2026-09-22": {
                "document": json.loads(day_body("SE4", TODAY)), "fetched_at": "2026-09-21T12:00:00+00:00",
                "provider": "relay",
            }}}
        )
        await domain_data(hass).price_refresh.async_refresh_area("SE4")
        await hass.async_block_till_done()
        device = price.runtime_data.device
        assert device.source is None and device.problem == "add_nordpool"
        assert device.diagnostics()["source"] is None
        assert repository.day_snapshot("SE4", TODAY).document is not None
        state = hass.states.get(entity(hass, price.entry_id, "price_now", "sensor"))
        assert state is not None and state.state not in ("unknown", "unavailable")
        assert state.attributes["source"] is None
    assert _issues(hass) == []


async def test_removing_the_price_takes_its_repair_along(hass: HomeAssistant) -> None:
    price = await add_price(hass, area="ES")
    await domain_data(hass).price_refresh.async_refresh_area("ES")
    await hass.async_block_till_done()
    assert _issues(hass) == [f"price_source_missing_{price.entry_id}"]
    assert await hass.config_entries.async_unload(price.entry_id)
    await hass.async_block_till_done()
    assert _issues(hass) == []


def test_the_fixtures_day_is_today() -> None:
    # The held SE4 file is for TODAY; the frozen NOW falls on it.
    assert NOW.astimezone(ZoneInfo("Europe/Stockholm")).date() == TODAY
