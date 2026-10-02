"""The relay's history profile (`/v1/<area>/profile.json`, contract v1): parsing and the repository.

The parser is judged on documents (good, malformed in each way the contract can be broken), the
repository on the wire stub and an injected clock: once per area per local day, the relay's 404 as a
definite "no usable profile", a held copy kept through a transport failure but not past two days, and
the store round trip.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.pricing.price_repository import (
    PROFILE_MAX_AGE,
    PROFILE_RETRY_AFTER,
    PriceRepository,
)
from custom_components.spotnav.pricing.relay_contract import (
    PriceProfile,
    RelayParseError,
    parse_profile,
)

from .relay import (
    BASE_URL,
    Clock,
    fixture,
    profile_document,
    profile_path,
    serve_profile,
    StoreDouble,
    StubTransport,
)

TODAY = date(2026, 10, 1)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def repository(hass: HomeAssistant, transport: StubTransport, clock: Clock, now: datetime) -> PriceRepository:
    clock.now = now
    return PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=StoreDouble())


# ------------------------------------------------------------------------------------- the parser


def test_the_checked_in_profile_parses_into_weekday_hours() -> None:
    profile = parse_profile(json.loads(fixture("profile_SE4.json")), area_id="SE4")

    assert isinstance(profile, PriceProfile)
    assert (profile.area_id, profile.tz, profile.weeks) == ("SE4", "Europe/Stockholm", 4)
    assert (profile.from_date, profile.to_date) == (date(2026, 9, 4), date(2026, 10, 1))
    assert profile.generated == datetime(2026, 10, 1, 14, 5, tzinfo=timezone(timedelta(hours=2)))
    sunday_night = profile.hour(7, 3)
    assert sunday_night is not None and (sunday_night.median, sunday_night.std, sunday_night.n) == (0.03, 0.01, 16)
    # An hour the relay omitted (fewer than 8 samples) is absent, not zero.
    assert profile.hour(1, 12) is None
    assert len(profile.hours) == 10


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda d: d.update(v=2), "unsupported_version"),
        (lambda d: d.update(v=True), "invalid_field"),
        (lambda d: d.pop("v"), "missing_field"),
        (lambda d: d.update(area="SE3"), "area_mismatch"),
        (lambda d: d.update(tz="Mars/Olympus"), "invalid_field"),
        (lambda d: d.update(unit="SEK/kWh"), "unit_mismatch"),
        (lambda d: d.update(generated="2026-10-01T14:05:00"), "invalid_timestamp"),
        (lambda d: d.update(generated=None), "missing_field"),
        (lambda d: d.update(**{"from": "2026-10-02", "to": "2026-10-01"}), "invalid_field"),
        (lambda d: d.update(**{"to": "tomorrow"}), "invalid_field"),
        (lambda d: d.update(weeks=0), "invalid_field"),
        (lambda d: d.update(weeks="four"), "invalid_field"),
        (lambda d: d.update(hours={}), "invalid_field"),
        (lambda d: d["hours"].append(deepcopy(d["hours"][0])), "duplicate_hour"),
        (lambda d: d["hours"][0].update(weekday=0), "invalid_field"),
        (lambda d: d["hours"][0].update(weekday=8), "invalid_field"),
        (lambda d: d["hours"][0].update(hour=24), "invalid_field"),
        (lambda d: d["hours"][0].update(hour=1.5), "invalid_field"),
        (lambda d: d["hours"][0].update(n=0), "invalid_field"),
        (lambda d: d["hours"][0].update(median="0.1"), "invalid_number"),
        (lambda d: d["hours"][0].update(median=True), "invalid_number"),
        (lambda d: d["hours"][0].update(std=-0.01), "invalid_number"),
        (lambda d: d["hours"][0].update(std=float("inf")), "invalid_number"),
        (lambda d: d["hours"][0].pop("std"), "missing_field"),
        (lambda d: d["hours"].__setitem__(0, "not an object"), "not_an_object"),
    ],
)
def test_a_malformed_profile_is_refused_by_a_stable_code(mutate: Any, code: str) -> None:
    document = json.loads(fixture("profile_SE4.json"))
    mutate(document)
    with pytest.raises(RelayParseError) as error:
        parse_profile(document, area_id="SE4")
    assert error.value.code == code


def test_a_profile_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(RelayParseError) as error:
        parse_profile([], area_id="SE4")
    assert error.value.code == "not_an_object"


# --------------------------------------------------------------------------------- the repository


async def test_a_profile_is_fetched_once_per_area_per_local_day(
    repository: PriceRepository, transport: StubTransport, clock: Clock
) -> None:
    serve_profile(transport)

    first = await repository.async_get_profile("SE4", TODAY)
    again = await repository.async_get_profile("SE4", TODAY)
    assert first is not None and again is first
    assert transport.call_count(profile_path()) == 1
    assert repository.profile_for("SE4") is first

    # The next local day asks again, and only once.
    serve_profile(transport, generated="2026-10-02T14:05:00+02:00", to="2026-10-02")
    clock.now = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)
    newer = await repository.async_get_profile("SE4", TODAY + timedelta(days=1))
    await repository.async_get_profile("SE4", TODAY + timedelta(days=1))
    assert newer is not first and newer is not None and newer.to_date == date(2026, 10, 2)
    assert transport.call_count(profile_path()) == 2


async def test_the_relays_404_is_a_definite_no_profile_and_drops_a_held_copy(
    repository: PriceRepository, transport: StubTransport
) -> None:
    serve_profile(transport)
    assert await repository.async_get_profile("SE4", TODAY) is not None

    transport.serve(profile_path(), 404, "not found")
    assert await repository.async_get_profile("SE4", TODAY + timedelta(days=1)) is None
    assert repository.profile_for("SE4") is None
    # A 404 is an answer: not asked again the same day.
    await repository.async_get_profile("SE4", TODAY + timedelta(days=1))
    assert transport.call_count(profile_path()) == 2
    summary = repository.profile_summary("SE4")
    assert summary["held"] is False and summary["usable"] is False
    assert summary["checked_day"] == "2026-10-02"


async def test_an_area_the_relay_has_no_profile_for_is_simply_none(
    repository: PriceRepository, transport: StubTransport
) -> None:
    assert await repository.async_get_profile("DK1", TODAY) is None
    assert repository.profile_summary("DK1")["held"] is False


async def test_a_transport_failure_keeps_the_held_copy_and_retries_only_after_the_pause(
    repository: PriceRepository, transport: StubTransport, clock: Clock
) -> None:
    serve_profile(transport)
    held = await repository.async_get_profile("SE4", TODAY)
    assert held is not None

    transport.serve(profile_path(), 503, "busy")
    tomorrow = TODAY + timedelta(days=1)
    assert await repository.async_get_profile("SE4", tomorrow) is held
    assert await repository.async_get_profile("SE4", tomorrow) is held
    assert transport.call_count(profile_path()) == 2, "one failed try, then a pause"

    clock.advance(seconds=PROFILE_RETRY_AFTER.total_seconds() + 1)
    serve_profile(transport, generated="2026-10-02T09:00:00+02:00", to="2026-10-02")
    fresh = await repository.async_get_profile("SE4", tomorrow)
    assert fresh is not held and fresh is not None
    assert transport.call_count(profile_path()) == 3


async def test_an_invalid_body_never_replaces_the_good_one(
    repository: PriceRepository, transport: StubTransport
) -> None:
    serve_profile(transport)
    held = await repository.async_get_profile("SE4", TODAY)

    broken = profile_document()
    broken["hours"][0]["std"] = -1
    transport.serve(profile_path(), 200, json.dumps(broken))
    assert await repository.async_get_profile("SE4", TODAY + timedelta(days=1)) is held


async def test_a_profile_older_than_two_days_is_not_used(
    repository: PriceRepository, transport: StubTransport, clock: Clock
) -> None:
    serve_profile(transport)
    assert await repository.async_get_profile("SE4", TODAY) is not None

    clock.now = datetime(2026, 10, 1, 14, 5, tzinfo=timezone(timedelta(hours=2))) + PROFILE_MAX_AGE
    assert repository.profile_for("SE4") is None
    assert repository.profile_summary("SE4")["held"] is True
    assert repository.profile_summary("SE4")["usable"] is False


async def test_the_profile_survives_a_restart_through_the_store(
    hass: HomeAssistant, transport: StubTransport, clock: Clock, now: datetime
) -> None:
    clock.now = now
    store = StoreDouble()
    first = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=store)
    serve_profile(transport)
    await first.async_get_profile("SE4", TODAY)
    assert store.payload["profiles"]["SE4"]["document"]["weeks"] == 4

    second = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=store)
    await second.async_restore()
    restored = second.profile_for("SE4")
    assert restored is not None and restored.to_date == date(2026, 10, 1)
    # The restore asked for nothing: the day's request is still owed.
    assert transport.call_count(profile_path()) == 1


async def test_a_stored_profile_that_fails_the_parser_is_ignored(
    hass: HomeAssistant, transport: StubTransport, clock: Clock, now: datetime
) -> None:
    clock.now = now
    bad = profile_document()
    bad["weeks"] = 0
    store = StoreDouble(
        {
            "schema": 1,
            "catalogue": None,
            "index": None,
            "days": {},
            "profiles": {"SE4": {"document": bad, "fetched_at": now.isoformat()}},
        }
    )
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=store)
    await repository.async_restore()
    assert repository.profile_for("SE4") is None


async def test_the_profile_summary_carries_no_price_rows(
    repository: PriceRepository, transport: StubTransport
) -> None:
    serve_profile(transport)
    await repository.async_get_profile("SE4", TODAY)
    summary = repository.profile_summary("SE4")
    assert summary["hour_entries"] == 168 and summary["weeks"] == 4
    assert all(not isinstance(value, (list, dict)) for value in summary.values())
