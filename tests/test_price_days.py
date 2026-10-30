"""The webhook's `price_days` read: the charger's held market-day files and profile, as the relay serves them.

The answers are pinned under `tests/fixtures/webhook/price_days_*.json` (`SPOTNAV_WRITE_FIXTURES=1` rewrites them),
which the app vendors: each file in them must read through the relay's own parsers.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.price_days import (
    ERROR_NO_PRICE_AREA,
    ERROR_NO_PRICES,
    ERROR_UNKNOWN_PRICE_AREA,
    async_price_days_answer,
    covering_market_days,
    PriceDaysRefusal,
)
from custom_components.spotnav.pricing.relay_contract import (
    parse_catalogue,
    parse_day,
    parse_profile,
)
from custom_components.spotnav.runtime import domain_data

from .relay import Clock, hold_history, SE4, serve, StubTransport, TODAY, TOMORROW
from .test_price_device import add_price
from .test_price_provider_nordpool import rows, World
from .test_relay_v2 import OCT4, OCT5, repository_for, serve_v2
from .test_webhook_parity import _pinned, post
from .world import setup_charger

pytestmark = pytest.mark.usefixtures("offline_relay")

NOW = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
UTC = timezone.utc


def envelope(answer: dict[str, Any]) -> dict[str, Any]:
    """An answer as the webhook sends it."""
    return {"ok": True, "action": "price_days", **answer}


def read_back(answer: dict[str, Any]) -> None:
    """Every part of an answer reads through the relay's own parsers, as the app reads it."""
    area = parse_catalogue({"v": 2, "generated": "2026-10-30T00:00:00Z", "areas": [answer["area"]]}, version=2)
    entry = area.area(answer["area"]["id"])
    assert entry is not None
    for key, document in answer["files"].items():
        parsed = parse_day(document, area_id=entry.id, day=date.fromisoformat(key))
        assert parsed.tz == entry.market_tz, "every file is dated on the area's market calendar"
    if answer["profile"] is not None:
        parse_profile(answer["profile"], area_id=entry.id)


async def _charger_on_se4(hass: HomeAssistant) -> None:
    await setup_charger(hass, entry_id="entry_days", webhook_id="webhook-days")
    await add_price(hass, area=SE4)


# ------------------------------------------------------------------ over the webhook


async def test_a_single_calendar_area_answers_todays_and_tomorrows_files(
    hass: HomeAssistant, hass_client_no_auth, offline_relay: StubTransport
) -> None:
    with freeze_time(NOW):
        await _charger_on_se4(hass)
        repository = domain_data(hass).price_repository
        for day in (TODAY, TOMORROW):
            await repository.async_get_file(SE4, day)
        client = await hass_client_no_auth()
        status, body = await post(client, "webhook-days", {"action": "price_days", "api_version": 1})

    assert status == 200 and body["ok"] is True and body["action"] == "price_days" and body["v"] == 1
    assert body["area"]["id"] == SE4 and body["area"]["market_tz"] == body["area"]["tz"] == "Europe/Stockholm"
    assert body["provider"] == {
        "key": "relay", "name": "SpotNav relay", "url": "https://spotnav.sensnology.se", "notice": None
    }
    assert (body["state"], body["reason"], body["waiting_for_tomorrow"]) == ("ready", "ready", False)
    assert body["publication"] == body["area"]["publication"]
    assert list(body["files"]) == ["2026-09-22", "2026-09-23"]
    # Exactly what the relay served.
    assert body["files"]["2026-09-22"] == json.loads(offline_relay.routes["/v1/SE4/2026/09-22.json"][1][0])
    # Two days held: no profile yet.
    assert body["profile"] is None
    read_back(body)
    _pinned("price_days_relay_se4.json", body)


async def test_the_profile_is_the_relays_profile_shape(
    hass: HomeAssistant, hass_client_no_auth, offline_relay: StubTransport
) -> None:
    with freeze_time(NOW):
        await _charger_on_se4(hass)
        repository = domain_data(hass).price_repository
        hold_history(repository, SE4, to=TODAY.isoformat())
        await repository.async_get_file(SE4, TODAY)
        client = await hass_client_no_auth()
        status, body = await post(client, "webhook-days", {"action": "price_days"})
        built = repository.profile_for(SE4)

    assert status == 200 and built is not None
    profile = body["profile"]
    assert set(profile) == {"v", "area", "tz", "unit", "generated", "from", "to", "weeks", "hours"}
    assert (profile["v"], profile["area"], profile["unit"], profile["weeks"]) == (1, SE4, "EUR/kWh", 4)
    assert (profile["from"], profile["to"]) == ("2026-08-26", "2026-09-22")
    assert parse_profile(profile, area_id=SE4) == built
    keys = [(row["weekday"], row["hour"]) for row in profile["hours"]]
    assert keys == sorted(keys) and len(keys) == 7 * 24


async def test_a_charger_with_no_area_is_refused(hass: HomeAssistant, hass_client_no_auth) -> None:
    await setup_charger(hass, entry_id="entry_days", webhook_id="webhook-days")
    client = await hass_client_no_auth()

    status, body = await post(client, "webhook-days", {"action": "price_days"})

    assert (status, body) == (400, {"ok": False, "error": ERROR_NO_PRICE_AREA, "action": "price_days"})
    _pinned("price_days_no_price_area.json", body)


async def test_nothing_held_is_refused_and_asks_for_nothing(
    hass: HomeAssistant, hass_client_no_auth, offline_relay: StubTransport
) -> None:
    serve(offline_relay, days=())
    with freeze_time(NOW):
        await _charger_on_se4(hass)
        calls = len(offline_relay.calls)
        client = await hass_client_no_auth()
        status, body = await post(client, "webhook-days", {"action": "price_days"})

    assert (status, body) == (400, {"ok": False, "error": ERROR_NO_PRICES, "action": "price_days"})
    assert len(offline_relay.calls) == calls, "a read never asks the relay"
    _pinned("price_days_no_prices.json", body)


@pytest.mark.parametrize("version", [2, "1", True, None])
async def test_another_api_version_is_refused(hass: HomeAssistant, hass_client_no_auth, version: Any) -> None:
    await setup_charger(hass, entry_id="entry_days", webhook_id="webhook-days")
    client = await hass_client_no_auth()

    status, body = await post(client, "webhook-days", {"action": "price_days", "api_version": version})

    assert (status, body["error"], body["action"]) == (400, "spotnav_unsupported_api_version", "price_days")


async def test_the_dashboard_says_the_webhook_answers_price_days(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await setup_charger(hass, entry_id="entry_days", webhook_id="webhook-days")
    client = await hass_client_no_auth()

    status, body = await post(client, "webhook-days", {"action": "dashboard", "api_version": 1})

    assert status == 200 and body["charger"]["capabilities"]["price_days"] is True


# ------------------------------------------------------------------ calendars


def test_the_files_covering_today_and_tomorrow() -> None:
    stockholm = "Europe/Stockholm"
    assert covering_market_days(TODAY, stockholm, stockholm) == (TODAY, TOMORROW)
    # Helsinki on CET days: the last hour of yesterday's file, then today's, then tomorrow's.
    assert covering_market_days(date(2026, 10, 30), "Europe/Helsinki", "Europe/Brussels") == (
        date(2026, 10, 29), date(2026, 10, 30), date(2026, 10, 31)
    )
    # London on Paris days: today's, tomorrow's and the next.
    assert covering_market_days(OCT4, "Europe/London", "Europe/Paris") == (OCT4, OCT5, date(2026, 10, 6))


async def test_finland_on_nord_pool_answers_its_cet_files(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    world = World(hass, Clock(datetime(2026, 10, 30, 10, 0, tzinfo=UTC)), transport)
    await world.start()
    try:
        for day in (date(2026, 10, 29), date(2026, 10, 30)):
            world.service.serve("FI", day, rows(day))
        await world.repository.async_get_day("FI", date(2026, 10, 30))

        # 12:00 in Helsinki, before Nord Pool's 12:45 in Brussels: today's two CET files, tomorrow not out.
        answer = await async_price_days_answer(world.repository, "FI", world.clock.now)
        assert not isinstance(answer, PriceDaysRefusal)
        assert answer["area"]["tz"] == "Europe/Helsinki" and answer["area"]["market_tz"] == "Europe/Brussels"
        assert answer["area"]["source"] == {"name": "Nord Pool", "url": "https://www.nordpoolgroup.com/"}
        assert answer["provider"]["key"] == "nordpool" and answer["provider"]["notice"] is None
        assert answer["publication"] == {"time": "12:45", "tz": "Europe/Brussels"}
        assert list(answer["files"]) == ["2026-10-29", "2026-10-30"]
        assert answer["files"]["2026-10-30"]["market_tz"] == "Europe/Brussels"
        assert (answer["state"], answer["waiting_for_tomorrow"]) == ("waiting_for_tomorrow", True)
        assert answer["profile"] is None
        read_back(answer)

        # After the publication: the third file, and tomorrow in Helsinki is whole.
        world.service.serve("FI", date(2026, 10, 31), rows(date(2026, 10, 31)))
        world.clock.now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
        await world.repository.async_get_file("FI", date(2026, 10, 31))
        answer = await async_price_days_answer(world.repository, "FI", world.clock.now)
        assert not isinstance(answer, PriceDaysRefusal)
        assert list(answer["files"]) == ["2026-10-29", "2026-10-30", "2026-10-31"]
        assert (answer["state"], answer["reason"], answer["waiting_for_tomorrow"]) == ("ready", "ready", False)
        read_back(answer)
        _pinned("price_days_nordpool_fi.json", envelope(answer))
    finally:
        await world.stop()


async def test_a_london_day_from_the_relays_paris_files(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    # 23:30 BST on the 4th: today in London is the 4th's and 5th's Paris files, tomorrow also needs the 6th's.
    clock = Clock(datetime(2026, 10, 4, 22, 30, tzinfo=UTC))
    serve_v2(transport)
    repository = repository_for(hass, transport, clock)
    await repository.async_get_catalogue()
    for day in (OCT4, OCT5):
        await repository.async_get_file("GB-C", day)

    answer = await async_price_days_answer(repository, "GB-C", clock.now)

    assert not isinstance(answer, PriceDaysRefusal)
    area = answer["area"]
    assert (area["tz"], area["market_tz"]) == ("Europe/London", "Europe/Paris") and "eic" not in area
    assert area["included"] == ["vat", "tax", "grid_fee"]
    assert area["source"]["name"] == "Octopus Energy (Agile)", "the relay's own attribution"
    assert answer["provider"]["key"] == "relay"
    assert list(answer["files"]) == ["2026-10-04", "2026-10-05"]
    assert answer["state"] == "ready"
    read_back(answer)
    _pinned("price_days_relay_gb.json", envelope(answer))

    # An area no list names has no answer.
    assert await async_price_days_answer(repository, "XX9", clock.now) == PriceDaysRefusal(ERROR_UNKNOWN_PRICE_AREA)


async def test_the_answer_without_a_repository_or_an_area(hass: HomeAssistant) -> None:
    assert await async_price_days_answer(None, SE4, NOW) == PriceDaysRefusal(ERROR_NO_PRICES)
    assert await async_price_days_answer(None, None, NOW) == PriceDaysRefusal(ERROR_NO_PRICE_AREA)
