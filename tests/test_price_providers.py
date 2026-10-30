"""The provider seam under the repository: the router that picks a provider, and what the repository does with one.

Without any provider the router answers none for every area and the repository asks nothing; with one, a file comes
back as a raw document or as a stable code, never as an exception, and is filed under the provider that sent it.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.spotnav.pricing.price_repository import PriceRepository
from custom_components.spotnav.pricing.providers import choose_provider, ProviderRouter
from .relay import BUNDLED, Clock, StoreDouble, stub_repository, StubTransport, TODAY


def test_an_area_no_provider_serves_has_none() -> None:
    assert choose_provider(
        nordpool_serves=True, nordpool_loaded=False, entsoe_serves=True, entsoe_key_usable=False
    ) is None
    assert choose_provider(
        nordpool_serves=False, nordpool_loaded=True, entsoe_serves=False, entsoe_key_usable=True
    ) is None


async def test_the_router_without_providers_answers_none_for_every_area(hass: HomeAssistant) -> None:
    router = ProviderRouter()
    assert {router.provider_for(area) for area in ("SE4", "FI", "GB-A", "unknown")} == {None}
    status = router.source_status("SE4")
    assert (status.key, status.name, status.url) == (None, None, None)


async def test_an_area_with_no_provider_asks_nothing_and_says_so(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    store = StoreDouble()
    repository = PriceRepository(hass, now=clock, store=store, bundled=BUNDLED)

    snapshot = await repository.async_get_day("SE4", TODAY)

    assert transport.calls == []
    assert snapshot.state == "unavailable" and snapshot.document is None
    assert snapshot.attempt_error == "source_missing"
    assert store.saves == 0


async def test_the_repository_asks_the_router_and_files_what_the_provider_answered(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    transport.serve_area()
    store = StoreDouble()
    repository = stub_repository(hass, transport, now=clock, store=store)

    snapshot = await repository.async_get_day("SE4", TODAY)

    assert snapshot.state == "ready" and snapshot.source == "network"
    assert store.payload["days"]["SE4|Europe/Stockholm|2026-09-22"]["provider"] == "stub"
    assert "relay" not in store.payload
