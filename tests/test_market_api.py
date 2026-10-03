"""The market-options read: the catalogue Home Assistant holds, and the charger's stored choice.

Over Home Assistant's real authenticated socket, like the settings commands: scoping is exercised
through the real config-entry registry and permissions through the real handshake. The relay is the
reviewed offline stub, so nothing here touches a network, and the catalogue itself is the checked-in
document, parsed -- the tests compare the answer to the *parsed* model rather than to raw JSON, which
is what makes "values come from `AreaEntry`" a claim about behaviour rather than about a fixture.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.spotnav.api.common import (
    ERROR_CHARGER_UNLOADED,
    ERROR_SITE_NOT_CHARGER,
    ERROR_UNKNOWN_CHARGER,
    ERROR_UNSUPPORTED_VERSION,
)
from custom_components.spotnav.api.market import MARKET_API_VERSION, market_options_payload
from custom_components.spotnav.pricing.price_repository import CatalogueSnapshot
from custom_components.spotnav.pricing.relay_contract import parse_catalogue
from tests.helpers import make_site_entry
from tests.relay import BASE_URL
from tests.world import setup_charger
from tests.relay import StubTransport, fixture
from .world import ws_call
from custom_components.spotnav.runtime import domain_data

pytestmark = pytest.mark.usefixtures("offline_relay")

#: The catalogue's own path, and the only one this read may ever touch.
AREAS_PATH = "/v1/areas.json"

_message_ids = itertools.count(1)


def read_message(
    entry_id: Any = "entry_a", api_version: Any = MARKET_API_VERSION
) -> dict[str, Any]:
    return {
        "id": 1,
        "type": "spotnav/get_market_options",
        "api_version": api_version,
        "charger_id": entry_id,
    }


def served_catalogue():
    """The served catalogue, *parsed*: the authority the command reads, not a second copy of it."""
    return parse_catalogue(json.loads(fixture("areas.json")))


async def read_payload(hass: HomeAssistant, client, entry_id: str = "entry_a") -> dict[str, Any]:
    frame = await ws_call(client, read_message(entry_id))
    assert frame["success"] is True, frame
    return frame["result"]


# ------------------------------------------------------- 1. contract, version, scope, permission


async def test_an_authenticated_non_admin_reads_the_held_catalogue(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass, hass_read_only_access_token)

    message = read_message(entry.entry_id)
    frame = await ws_call(client, message)

    # The transport frame first, then the contract value inside it.
    assert frame["success"] is True and frame["id"] == message["id"]
    payload = frame["result"]
    assert set(payload) == {"api_version", "state", "reason", "areas", "configured_area"}
    # This contract's own version, as a literal: not the dashboard's, the settings' or the action's.
    assert payload["api_version"] == MARKET_API_VERSION == 1
    assert payload["state"] == "ready" and payload["reason"] is None
    assert payload["configured_area"] is None
    # The relay's published order, which is the order a person compares the two lists in.
    catalogue = served_catalogue()
    assert [area["area_id"] for area in payload["areas"]] == [entry.id for entry in catalogue.areas]


async def test_every_area_carries_the_relays_own_metadata_and_nothing_else(
    hass: HomeAssistant, hass_ws_client
) -> None:
    await setup_charger(hass)
    client = await hass_ws_client(hass)

    payload = await read_payload(hass, client)
    by_id = {area["area_id"]: area for area in payload["areas"]}
    serialized = json.dumps(payload)

    for model in served_catalogue().areas:
        area = by_id[model.id]
        assert set(area) == {
            "area_id",
            "name",
            "countries",
            "timezone",
            "currency",
            "major_unit",
            "minor_unit",
            "suggestions",
            "market_timezone",
            "included",
            "source",
        }
        # A v1 list: one calendar, nothing included, no attribution.
        assert area["market_timezone"] == model.tz
        assert area["included"] == [] and area["source"] is None
        assert area["name"] == model.name
        assert area["countries"] == list(model.countries)
        assert area["timezone"] == model.tz
        assert area["currency"] == model.currency
        assert area["major_unit"] == model.major_unit
        assert area["minor_unit"] == model.minor_unit
        # Unconverted, and three states rather than two: absent stays null, a published zero stays 0.
        assert area["suggestions"] == {
            "vat_percent": model.vat_percent,
            "tax_minor": model.suggested_tax,
            "transfer_minor": model.suggested_grid_fee,
        }
        # An EIC is a network code this editor has no use for, and it alone is dropped.
        assert "eic" not in area
        assert model.eic not in serialized

    # A display label is not an identity: one major label, two currencies, and two spellings of the
    # hundredth part -- each exactly as the relay published it, none of them derived from another.
    assert by_id["DK1"]["major_unit"] == by_id["NO1"]["major_unit"] == "kr"
    assert [by_id[area]["currency"] for area in ("DK1", "NO1", "SE4")] == ["DKK", "NOK", "SEK"]
    assert [by_id[area]["minor_unit"] for area in ("DK1", "NO1", "SE4")] == ["øre", "øre", "öre"]
    # A published zero, a published figure and nothing published, all in one catalogue.
    assert by_id["DK1"]["suggestions"]["tax_minor"] == 0
    assert by_id["NO1"]["suggestions"] == {
        "vat_percent": 25.0,
        "tax_minor": 7.13,
        "transfer_minor": 0,
    }
    assert by_id["DE-LU"]["suggestions"] == {
        "vat_percent": None,
        "tax_minor": None,
        "transfer_minor": None,
    }
    # Nothing about this installation's own plumbing travels, in keys or in values.
    assert BASE_URL not in serialized
    assert "webhook" not in serialized.lower()


async def test_an_unsupported_version_gets_this_contracts_stable_code(
    hass: HomeAssistant, hass_ws_client
) -> None:
    await setup_charger(hass)
    client = await hass_ws_client(hass)

    # An integer version, and *only* an integer: `1.0` is a float that Python compares equal to `1`,
    # `True` and `False` are integer subclasses, and the rest are not integers at all. Every one of
    # them is refused by this contract's own stable code rather than by a generic format error.
    for version in (0, 2, 3, "1", "1.0", None, True, False, 1.0, 2.0, 1.5, [], {}):
        frame = await ws_call(client, read_message("entry_a", version))
        assert frame["success"] is False, version
        assert frame["error"]["code"] == ERROR_UNSUPPORTED_VERSION, version
        assert "result" not in frame

    # A request that omits the version entirely gets the same stable refusal: the command's schema
    # leaves `api_version` optional on purpose, so the version is judged in exactly one place. (A
    # missing `charger_id` is a different fact -- a request that names no charger at all.)
    frame = await ws_call(client, {"type": "spotnav/get_market_options", "charger_id": "entry_a"})
    assert frame["success"] is False
    assert frame["error"]["code"] == ERROR_UNSUPPORTED_VERSION
    assert "result" not in frame

    # And the one integer this contract speaks still answers, stating the same version it spoke.
    accepted = await ws_call(client, read_message("entry_a"))
    assert accepted["success"] is True
    assert accepted["result"]["api_version"] == MARKET_API_VERSION == 1
    assert type(accepted["result"]["api_version"]) is int


async def test_charger_scope_is_the_dashboards_four_rules(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger = await setup_charger(hass)
    unloaded = await setup_charger(
        hass, entry_id="entry_c", webhook_id="webhook-c", charge_control="switch.charger_c"
    )
    site = make_site_entry(hass, entry_id="site_a", charger_entry_ids=[charger.entry_id])
    assert await hass.config_entries.async_setup(site.entry_id)
    foreign = MockConfigEntry(domain="other_component", data={})
    foreign.add_to_hass(hass)
    assert await hass.config_entries.async_unload(unloaded.entry_id)
    await hass.async_block_till_done()

    client = await hass_ws_client(hass)
    cases = [
        ("entry_missing", ERROR_UNKNOWN_CHARGER),
        (foreign.entry_id, ERROR_UNKNOWN_CHARGER),
        (site.entry_id, ERROR_SITE_NOT_CHARGER),
        (unloaded.entry_id, ERROR_CHARGER_UNLOADED),
    ]
    for entry_id, code in cases:
        frame = await ws_call(client, read_message(entry_id))
        assert frame["success"] is False, entry_id
        assert frame["error"]["code"] == code, entry_id
        assert "result" not in frame
    # And the refusal is not a substitute for the scope this command accepts.
    assert (await ws_call(client, read_message(charger.entry_id)))["success"] is True


# -------------------------------------------- 2. the catalogue's own five states, and one reason


def test_the_payload_repeats_the_repositorys_state_and_bounds_its_reason() -> None:
    """The pure half: one state vocabulary, and two public reasons instead of internal codes.

    `loading` is in here on purpose. A read *awaits* the ensure attempt it starts with an empty cache,
    so it never answers while one is running -- but the state a payload can carry is the repository's
    own, and this proves the helper passes all five through rather than mapping them into new names.
    """

    def snapshot(**changes: Any) -> CatalogueSnapshot:
        base = CatalogueSnapshot(
            state="ready",
            catalogue=None,
            source=None,
            fetched_at=None,
            attempt_at=None,
            attempt_error=None,
            refreshing=False,
        )
        return replace(base, **changes)

    assert market_options_payload(None, "SE4") == {
        "api_version": MARKET_API_VERSION,
        "state": "unavailable",
        "reason": None,
        "areas": [],
        "configured_area": "SE4",
    }
    for state in ("loading", "ready", "stale", "unavailable", "invalid"):
        payload = market_options_payload(snapshot(state=state, refreshing=state == "loading"), "SE4")
        assert payload["state"] == state
        assert payload["reason"] is None
    # A fetch failure is "the relay could not be reached"; a rejection of its document is the other
    # public reason. Neither the code nor any part of an exception travels.
    for code in ("timeout", "network", "http_status", "too_large"):
        assert market_options_payload(snapshot(attempt_error=code), None)["reason"] == "offline"
    for code in ("not_json", "missing_field", "invalid_field", "area_mismatch"):
        assert market_options_payload(snapshot(attempt_error=code), None)["reason"] == "invalid"


async def test_a_held_catalogue_whose_next_attempt_failed_is_stale_and_still_usable(
    hass: HomeAssistant, hass_ws_client, transport: StubTransport
) -> None:
    await setup_charger(hass)
    client = await hass_ws_client(hass)
    assert (await read_payload(hass, client))["state"] == "ready"

    # The next attempt fails while last-good stays on record: stale, not empty.
    transport.serve(AREAS_PATH, 500, "the relay is unwell")
    repository = domain_data(hass).price_repository
    assert repository is not None
    assert await repository.async_get_catalogue(refresh=True) is not None

    payload = await read_payload(hass, client)
    assert payload["state"] == "stale" and payload["reason"] == "offline"
    assert [area["area_id"] for area in payload["areas"]] == [
        model.id for model in served_catalogue().areas
    ]
    # A held catalogue is never refreshed by a read: the two attempts above are the only two.
    assert transport.call_count(AREAS_PATH) == 2


async def test_nothing_held_is_an_honest_answer_and_never_a_compiled_fallback(
    hass: HomeAssistant, hass_ws_client, transport: StubTransport
) -> None:
    # The relay serves no catalogue at all, which is a transport failure and not an empty market.
    transport.routes.pop(AREAS_PATH)
    await setup_charger(hass)
    client = await hass_ws_client(hass)

    before = transport.call_count(AREAS_PATH)
    payload = await read_payload(hass, client)
    # Exactly one attempt, and it is the repository's coalesced ensure path -- no downloader of ours.
    assert transport.call_count(AREAS_PATH) == before + 1
    assert payload["state"] == "unavailable" and payload["reason"] == "offline"
    assert payload["areas"] == [] and payload["configured_area"] is None


async def test_a_catalogue_the_client_cannot_read_is_invalid_not_unavailable(
    hass: HomeAssistant, hass_ws_client, transport: StubTransport
) -> None:
    transport.serve(AREAS_PATH, 200, "{not json at all")
    await setup_charger(hass)
    client = await hass_ws_client(hass)

    payload = await read_payload(hass, client)
    assert payload["state"] == "invalid" and payload["reason"] == "invalid"
    assert payload["areas"] == []


# ------------------------------------ 3. the stored choice, offline and unlisted; one shared cache


async def test_the_stored_area_survives_an_offline_catalogue_and_an_unlisted_one(
    hass: HomeAssistant, hass_ws_client, transport: StubTransport
) -> None:
    entry = await setup_charger(hass)
    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(entry.entry_id, mutate=lambda s: replace(s, area_id="SE4"))
    client = await hass_ws_client(hass)

    listed = await read_payload(hass, client)
    assert listed["configured_area"] == "SE4"
    assert "SE4" in {area["area_id"] for area in listed["areas"]}

    # A catalogue that no longer publishes it: the choice is reported, and nothing is invented for it.
    document = json.loads(fixture("areas.json"))
    document["areas"] = [area for area in document["areas"] if area["id"] != "SE4"]
    transport.serve(AREAS_PATH, 200, json.dumps(document))
    repository = domain_data(hass).price_repository
    assert repository is not None
    refreshed = await repository.async_get_catalogue(refresh=True)
    assert refreshed.catalogue is not None and "SE4" not in refreshed.catalogue.area_ids

    unlisted = await read_payload(hass, client)
    assert unlisted["configured_area"] == "SE4"
    assert "SE4" not in {area["area_id"] for area in unlisted["areas"]}
    assert "SE4" not in json.dumps(unlisted["areas"])

    # And with the relay offline entirely, the same stored choice is still the answer.
    transport.routes.pop(AREAS_PATH)
    repository = domain_data(hass).price_repository
    await repository.async_get_catalogue(refresh=True)
    offline = await read_payload(hass, client)
    assert offline["configured_area"] == "SE4"
    assert "SE4" not in {area["area_id"] for area in offline["areas"]}


async def test_two_chargers_share_one_fetch_and_repeated_reads_leak_nothing(
    hass: HomeAssistant, hass_ws_client, transport: StubTransport
) -> None:
    first = await setup_charger(hass)
    second = await setup_charger(
        hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b"
    )
    client = await hass_ws_client(hass)
    manager = domain_data(hass).price_refresh
    assert manager is not None
    data_before = dict(vars(domain_data(hass)))

    before = transport.call_count(AREAS_PATH)
    for _ in range(3):
        for entry in (first, second):
            payload = await read_payload(hass, client, entry.entry_id)
            assert payload["state"] == "ready" and payload["areas"]
    # Six reads, two chargers, and not one extra request: a held catalogue is handed straight through,
    # and every read starts no fetch, no refresh, no timer and no second cache of its own.
    assert before >= 1, "the domain had already ensured a catalogue"
    assert transport.call_count(AREAS_PATH) == before
    # No subscription, no timer, no poller: the manager tracks exactly the areas it was asked for,
    # and a read never asks. This is the read-only claim, as an observable fact.
    assert manager.manager_snapshot().areas == ()
    # And nothing new appeared in the domain's own data: no listener, no second cache.
    assert dict(vars(domain_data(hass))) == data_before


async def test_the_read_writes_no_store_and_calls_no_service(
    hass: HomeAssistant, hass_ws_client, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await setup_charger(hass)
    store = domain_data(hass).auto_store
    assert store is not None
    before = store.settings(entry.entry_id)
    turn_on = async_mock_service(hass, "switch", "turn_on")

    saves: list[str] = []

    async def spy(self, data: Any) -> None:
        saves.append(self.key)

    monkeypatch.setattr(Store, "async_save", spy)
    client = await hass_ws_client(hass)
    calls_before = list(transport.calls)

    payload = await read_payload(hass, client)
    await hass.async_block_till_done()

    assert payload["state"] == "ready"
    assert saves == [], "a read never writes a store"
    assert store.settings(entry.entry_id) == before, "no field of the record moved"
    assert store.settings(entry.entry_id).revision == 0
    assert turn_on == [], "a read never calls a service"
    # Not one new request of any kind: no day, no index, no catalogue refresh, no charger, no planner.
    assert [call for call in transport.calls if call not in calls_before] == []


# ------------------------------------------------------- 4. one command for the domain, registered
