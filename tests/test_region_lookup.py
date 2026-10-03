"""`spotnav/find_region`: a Great Britain postcode to its price region, asked of Octopus by Home Assistant.

Octopus's grid-supply-point lookup is mocked; the relay is the stub. The postcode is personal data: it
must reach Octopus and nothing else, and never the log.
"""

from __future__ import annotations

import pytest
from aiohttp import ClientError
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.region import (
    OCTOPUS_GSP_URL,
    async_find_region,
    normalized_postcode,
    regions_from,
)
from custom_components.spotnav.pricing.price_repository import PriceRepository
from custom_components.spotnav.runtime import domain_data

from .relay import BASE_URL, Clock, StoreDouble, StubTransport
from .test_relay_v2 import serve_v2

POSTCODE = "SW1A 1AA"


@pytest.fixture
async def gb_catalogue(hass: HomeAssistant, transport: StubTransport, clock: Clock) -> PriceRepository:
    serve_v2(transport)
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=StoreDouble())
    await repository.async_get_catalogue(refresh=True)
    domain_data(hass).price_repository = repository
    return repository


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SW1A 1AA", "SW1A 1AA"),
        ("sw1a1aa", "SW1A 1AA"),
        ("  m1  1ae ", "M1 1AE"),
        ("EH99 1SP", "EH99 1SP"),
        ("12345", None),
        ("SW1A 1AA; DROP", None),
        ("", None),
        (None, None),
    ],
)
def test_a_postcode_is_read_loosely_and_anything_else_is_refused(raw, expected) -> None:
    assert normalized_postcode(raw) == expected


def test_octopus_group_ids_become_relay_area_ids_each_once() -> None:
    answer = {"results": [{"group_id": "_C"}, {"group_id": "_C"}, {"group_id": "_I"}, {"group_id": "_A"}]}
    # There is no group I (nor O): an id that is not a GSP group is not invented into an area.
    assert regions_from(answer) == ["GB-C", "GB-A"]
    with pytest.raises(ValueError):
        regions_from({"detail": "nope"})


async def test_a_postcode_finds_its_region_and_goes_to_octopus_only(
    hass: HomeAssistant, gb_catalogue: PriceRepository, transport: StubTransport, aioclient_mock, caplog
) -> None:
    aioclient_mock.get(OCTOPUS_GSP_URL, json={"count": 1, "results": [{"group_id": "_C"}]})
    calls_before = list(transport.calls)
    answer = await async_find_region(hass, "sw1a 1aa")
    assert answer == {"api_version": 1, "reason": None, "region": "GB-C", "regions": ["GB-C"]}
    # One request, to Octopus, with the postcode; the relay saw nothing new.
    assert aioclient_mock.call_count == 1
    method, url, *_ = aioclient_mock.mock_calls[0]
    assert str(url).startswith(OCTOPUS_GSP_URL) and url.query["postcode"] == "SW1A1AA"
    assert transport.calls == calls_before
    assert "SW1A" not in caplog.text


async def test_a_region_the_catalogue_does_not_list_is_not_offered(
    hass: HomeAssistant, gb_catalogue: PriceRepository, aioclient_mock
) -> None:
    # The fixture catalogue lists GB-C only.
    aioclient_mock.get(OCTOPUS_GSP_URL, json={"count": 1, "results": [{"group_id": "_P"}]})
    answer = await async_find_region(hass, POSTCODE)
    assert answer["reason"] == "not_found" and answer["region"] is None and answer["regions"] == []


async def test_an_unknown_postcode_is_not_found(
    hass: HomeAssistant, gb_catalogue: PriceRepository, aioclient_mock
) -> None:
    aioclient_mock.get(OCTOPUS_GSP_URL, json={"count": 0, "results": []})
    assert (await async_find_region(hass, POSTCODE))["reason"] == "not_found"


@pytest.mark.parametrize("failure", [{"status": 500}, {"exc": ClientError()}, {"text": "<html>"}, {"exc": TimeoutError()}])
async def test_an_unreachable_octopus_is_unavailable_and_the_postcode_stays_out_of_the_log(
    hass: HomeAssistant, gb_catalogue: PriceRepository, aioclient_mock, caplog, failure
) -> None:
    aioclient_mock.get(OCTOPUS_GSP_URL, **failure)
    answer = await async_find_region(hass, POSTCODE)
    assert answer["reason"] == "unavailable" and answer["regions"] == []
    assert "SW1A" not in caplog.text and "1AA" not in caplog.text


async def test_an_invalid_postcode_asks_nobody(hass: HomeAssistant, gb_catalogue: PriceRepository, aioclient_mock) -> None:
    answer = await async_find_region(hass, "not a postcode")
    assert answer["reason"] == "invalid_postcode"
    assert aioclient_mock.call_count == 0


async def test_the_command_answers_over_the_socket(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, aioclient_mock
) -> None:
    from .world import setup_charger, ws_call

    await setup_charger(hass)
    aioclient_mock.get(OCTOPUS_GSP_URL, json={"count": 1, "results": [{"group_id": "_C"}]})
    client = await hass_ws_client(hass, hass_read_only_access_token)
    frame = await ws_call(client, {"id": 1, "type": "spotnav/find_region", "api_version": 1, "postcode": POSTCODE})
    assert frame["success"] is True
    # The offline relay's catalogue is v1 (no Great Britain): nothing it does not list is offered.
    assert frame["result"]["reason"] == "not_found"
    refused = await ws_call(client, {"id": 2, "type": "spotnav/find_region", "api_version": 2, "postcode": POSTCODE})
    assert refused["success"] is False
