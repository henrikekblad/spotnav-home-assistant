"""The provider seam under the repository: the relay's wire as a provider, and the router that picks it.

Without a Nord Pool provider the router answers the relay for every area; what is tested here is the shape every
provider will share: a file comes back as a raw document or as a stable code, never as an exception.
"""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant

from custom_components.spotnav.pricing.price_repository import PriceRepository
from custom_components.spotnav.pricing.providers import DayFetch, ProviderRouter
from custom_components.spotnav.pricing.providers.relay import RelayProvider
from .relay import BASE_URL, Clock, StoreDouble, StubTransport, TODAY, day_body


async def test_the_relay_answers_a_file_as_its_raw_document(hass: HomeAssistant, transport: StubTransport) -> None:
    transport.serve(transport.day_path("SE4", TODAY), 200, day_body("SE4", TODAY))
    relay = RelayProvider(hass, base_url=BASE_URL, session=transport)

    fetch = await relay.async_fetch_day("SE4", TODAY)

    assert fetch == DayFetch("fetched", document=json.loads(day_body("SE4", TODAY)))
    assert transport.calls == ["/v1/SE4/2026/09-22.json"]


async def test_the_relay_answers_a_failure_as_a_code(hass: HomeAssistant, transport: StubTransport) -> None:
    relay = RelayProvider(hass, base_url=BASE_URL, session=transport)
    transport.serve(transport.day_path("SE4", TODAY), 200, "<html>proxy</html>")

    assert await relay.async_fetch_day("SE4", TODAY) == DayFetch("failed", code="not_json")
    transport.serve(transport.day_path("SE4", TODAY), 404, "missing")
    assert await relay.async_fetch_day("SE4", TODAY) == DayFetch("failed", code="http_status")
    body = day_body("SE4", TODAY).encode("utf-8")
    transport.serve_chunks(transport.day_path("SE4", TODAY), 200, [body[:64], body[64:]], fail_after=1)
    assert await relay.async_fetch_day("SE4", TODAY) == DayFetch("failed", code="network")


async def test_the_router_answers_the_relay_for_every_area(hass: HomeAssistant) -> None:
    relay = RelayProvider(hass, base_url=BASE_URL)
    router = ProviderRouter(relay)
    assert router.relay is relay
    assert {router.provider_for(area) for area in ("SE4", "FI", "GB-A", "unknown")} == {relay}
    assert relay.key == "relay" and relay.has_index is True


async def test_the_repository_asks_the_router_and_files_what_the_provider_answered(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    transport.serve_area()
    store = StoreDouble()
    router = ProviderRouter(RelayProvider(hass, base_url=BASE_URL, session=transport))
    repository = PriceRepository(hass, now=clock, store=store, router=router)

    snapshot = await repository.async_get_day("SE4", TODAY)

    assert snapshot.state == "ready" and snapshot.source == "network"
    assert store.payload["days"]["SE4|Europe/Stockholm|2026-09-22"]["provider"] == "relay"
