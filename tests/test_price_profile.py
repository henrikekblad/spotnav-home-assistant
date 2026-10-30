"""The history profile: the relay's document (`/v1/<area>/profile.json`, contract v1) and the repository's own.

The parser is judged on documents (good, malformed in each way the contract can be broken); the repository
on held day files and an injected clock: the profile is built from the market-day files of its window with
no request, kept until the window or a file in it changes, absent below 21 files, and blind to files on
another calendar than the area's.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.pricing.area_catalogue import async_load_bundled
from custom_components.spotnav.pricing.price_profile import build_profile
from custom_components.spotnav.pricing.price_repository import PriceRepository
from custom_components.spotnav.pricing.relay_contract import (
    PriceProfile,
    RelayParseError,
    parse_profile,
)

from .relay import (
    BASE_URL,
    Clock,
    fixture,
    history_days,
    profile_document,
    StoreDouble,
    StubTransport,
)

#: Sunday's night hours, in the profile's local weekday-hour.
SUNDAY_NIGHT = {(7, hour): 0.03 for hour in range(8)}


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
async def repository(hass: HomeAssistant, transport: StubTransport, clock: Clock, now: datetime) -> PriceRepository:
    clock.now = now
    return PriceRepository(
        hass, base_url=BASE_URL, session=transport, now=clock, store=StoreDouble(),
        bundled=await async_load_bundled(hass),
    )


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


async def hold(repository: PriceRepository, days: dict[str, Any]) -> None:
    await repository.async_restore()
    repository._adopt({"schema": 2, "days": days})  # noqa: SLF001 - the store's own path


async def test_the_profile_is_built_from_held_days_without_a_request(
    repository: PriceRepository, transport: StubTransport
) -> None:
    await hold(repository, history_days(to="2026-10-02", cheap=SUNDAY_NIGHT))

    profile = await repository.async_get_profile("SE4")

    assert profile is not None and transport.calls == []
    assert (profile.from_date, profile.to_date, profile.weeks) == (date(2026, 9, 5), date(2026, 10, 2), 4)
    assert profile.tz == "Europe/Stockholm" and len(profile.hours) == 168
    sunday_night, monday_noon = profile.hours[(7, 3)], profile.hours[(1, 12)]
    assert (sunday_night.median, sunday_night.std, sunday_night.n) == (0.03, 0.01, 16)
    assert (monday_noon.median, monday_noon.std, monday_noon.n) == (0.10, 0.01, 16)
    # The same as building it from those files directly: the repository passes the market-day files.
    documents = {cached.parsed.day: cached.parsed for files in repository._days.values() for cached in files.values()}
    assert build_profile("SE4", "Europe/Stockholm", "Europe/Stockholm", repository._now(), documents) == profile


async def test_the_profile_is_kept_until_the_window_or_a_file_in_it_changes(
    repository: PriceRepository, clock: Clock
) -> None:
    await hold(repository, history_days(to="2026-10-02"))
    first = repository.profile_for("SE4")
    assert first is not None and repository.profile_for("SE4") is first

    # A new file in the window builds it again.
    today = history_days(to="2026-10-03")["SE4|Europe/Stockholm|2026-10-02"]
    await hold(repository, {"SE4|Europe/Stockholm|2026-10-02": {**today, "fetched_at": "2026-10-02T12:00:00+00:00"}})
    second = repository.profile_for("SE4")
    assert second is not None and second is not first

    # So does the next market day, which moves the window.
    clock.now = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)
    third = repository.profile_for("SE4")
    assert third is not None and third is not second and third.to_date == date(2026, 10, 3)


async def test_twenty_files_are_no_profile_and_twenty_one_are(repository: PriceRepository) -> None:
    days = sorted(history_days(to="2026-10-02").items())
    await hold(repository, dict(days[-20:]))
    assert repository.profile_for("SE4") is None
    assert repository.profile_summary("SE4") | {"area": None} == {
        "area": None, "usable": False, "days_held": 20, "generated": None, "from": None, "to": None,
        "weeks": None, "hour_entries": None,
    }

    await hold(repository, dict(days[-21:]))
    assert repository.profile_for("SE4") is not None
    assert repository.profile_summary("SE4")["days_held"] == 21


async def test_an_area_no_catalogue_lists_has_no_profile(
    hass: HomeAssistant, transport: StubTransport, clock: Clock, now: datetime
) -> None:
    clock.now = now
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=StoreDouble())
    await hold(repository, history_days(to="2026-10-02"))
    assert await repository.async_get_profile("SE4") is None
    assert transport.calls == []


async def test_files_on_another_calendar_are_not_counted(repository: PriceRepository) -> None:
    """Finland is read on the relay's Helsinki days: CET files held beside them are not that calendar's days."""
    helsinki = {}
    for key, entry in history_days(to="2026-10-02").items():
        document = dict(entry["document"], area="FI", tz="Europe/Brussels")
        helsinki[key.replace("SE4|Europe/Stockholm", "FI|Europe/Brussels")] = {**entry, "document": document}
    await hold(repository, helsinki)
    assert repository.profile_for("FI") is None


async def test_a_split_calendar_area_is_built_from_its_market_day_files(repository: PriceRepository) -> None:
    """Portugal: Madrid files, keyed by Madrid date, grouped by Lisbon weekday-hour."""
    files = {}
    for key, entry in history_days(to="2026-10-02").items():
        document = dict(entry["document"], area="PT", tz="Europe/Lisbon", market_tz="Europe/Madrid")
        files[key.replace("SE4|Europe/Stockholm", "PT|Europe/Madrid")] = {**entry, "document": document}
    await hold(repository, files)

    profile = repository.profile_for("PT")

    assert profile is not None and profile.tz == "Europe/Lisbon"
    documents = {
        day: cached.parsed for (area, day), held in repository._days.items() if area == "PT" for cached in held.values()
    }
    assert all(document.tz == "Europe/Madrid" for document in documents.values())
    assert build_profile("PT", "Europe/Lisbon", "Europe/Madrid", repository._now(), documents) == profile
    # Madrid's midnight is 23:00 in Lisbon: the first quarter of a Madrid Thursday is a Lisbon Wednesday.
    assert profile.hours[(3, 23)].n == 16


async def test_a_stored_relay_profile_is_not_read_any_more(
    hass: HomeAssistant, transport: StubTransport, clock: Clock, now: datetime
) -> None:
    clock.now = now
    store = StoreDouble(
        {
            "schema": 1,
            "catalogue": None,
            "index": None,
            "days": {},
            "profiles": {"SE4": {"document": profile_document(), "fetched_at": now.isoformat()}},
        }
    )
    repository = PriceRepository(
        hass, base_url=BASE_URL, session=transport, now=clock, store=store, bundled=await async_load_bundled(hass)
    )
    assert await repository.async_get_profile("SE4") is None
    await repository._persist()
    assert "profiles" not in store.payload["relay"]
    assert transport.calls == []


async def test_the_profile_summary_carries_no_price_rows(repository: PriceRepository) -> None:
    await hold(repository, history_days(to="2026-10-02"))
    summary = repository.profile_summary("SE4")
    assert summary["usable"] is True and summary["days_held"] == 27
    assert summary["hour_entries"] == 168 and summary["weeks"] == 4
    assert all(not isinstance(value, (list, dict)) for value in summary.values())
