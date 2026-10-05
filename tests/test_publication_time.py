"""Each area's own expected publication time (`publication` in `/v2/areas.json`).

ENTSO-E areas publish tomorrow around 13:00 Brussels, Octopus Agile (GB-*) around 16:00 UK and Spain's PVPC
around 20:15 Madrid. The area list states it per area; an area that states nothing, or states it in a shape this
client cannot read, is expected at 13:00 Brussels and is never dropped for it. The time decides two things: when
the waiting text says the prices are due (the time plus a 45-minute margin, built in the stated zone) and when
the refresh polls the index often (from 5 minutes before to 2 hours after, while tomorrow is missing).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_controller import expected_publication
from custom_components.spotnav.planning.price_wait import expected_publication_at
from custom_components.spotnav.planning.status_compose import PlanningFacts, StatusFacts, compose_status
from custom_components.spotnav.pricing.price_refresh import (
    HEALTHY_POLL_SECONDS,
    PUBLICATION_POLL_SECONDS,
    PriceRefreshManager,
    in_publication_window,
)
from custom_components.spotnav.pricing.price_repository import PriceRepository
from custom_components.spotnav.pricing.relay_contract import (
    DEFAULT_PUBLICATION,
    AreaPublication,
    parse_catalogue,
)

from .harness import Harness, assert_nothing_executed
from .relay import SE4, TODAY, BASE_URL, Clock, FakeScheduler, StoreDouble, StubTransport, cheap_night_day, fixture, serve

V2 = Path(__file__).parent / "fixtures" / "relay_v2"
LONDON = "Europe/London"
MADRID = "Europe/Madrid"
BRUSSELS = "Europe/Brussels"
GB = AreaPublication(local_time=time(16, 0), tz=LONDON)
PVPC = AreaPublication(local_time=time(20, 15), tz=MADRID)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def stated() -> dict[str, Any]:
    return json.loads(fixture("areas-v2-publication.json"))


# --- the area list -------------------------------------------------------------------------------


def test_each_area_states_its_own_time_and_an_area_without_one_is_expected_at_13_brussels() -> None:
    catalogue = parse_catalogue(stated(), version=2)
    by_id = {entry.id: entry.publication for entry in catalogue.areas}
    assert by_id == {
        "ES-PVPC": PVPC,
        "GB-C": GB,
        "PT": DEFAULT_PUBLICATION,
        "SE4": AreaPublication(local_time=time(13, 0), tz=BRUSSELS),
    }
    assert DEFAULT_PUBLICATION == AreaPublication(local_time=time(13, 0), tz=BRUSSELS)
    # The relay's own list states every area's time: Agile at 16:00 London, ENTSO-E at 13:00 Brussels.
    relay = parse_catalogue(json.loads((V2 / "areas-v2.json").read_text("utf-8")), version=2)
    assert {entry.id: entry.publication for entry in relay.areas} == {
        "GB-C": GB,
        "PT": DEFAULT_PUBLICATION,
        "SE4": DEFAULT_PUBLICATION,
    }


def test_a_v1_list_never_carries_the_field_and_ignores_it_if_it_did() -> None:
    document = json.loads((V2 / "areas-v1.json").read_text("utf-8"))
    for area in document["areas"]:
        area["publication"] = {"time": "16:00", "tz": LONDON}
    catalogue = parse_catalogue(document)
    assert {entry.publication for entry in catalogue.areas} == {DEFAULT_PUBLICATION}


def test_an_unknown_field_is_accepted_beside_the_publication() -> None:
    document = stated()
    for area in document["areas"]:
        area["something_new"] = {"nested": [1, 2, 3]}
        area["publication"] = {**area.get("publication", {"time": "13:00", "tz": BRUSSELS}), "window": 30}
    catalogue = parse_catalogue(document, version=2)
    assert [entry.id for entry in catalogue.areas] == ["ES-PVPC", "GB-C", "PT", "SE4"]
    assert catalogue.area("GB-C").publication == GB


@pytest.mark.parametrize(
    "publication",
    [
        None,
        "16:00",
        16,
        [],
        {},
        {"time": "16:00"},
        {"tz": LONDON},
        {"time": "16:00", "tz": "Mars/Olympus"},
        {"time": "16:00", "tz": ""},
        {"time": "16:00", "tz": 1},
        {"time": "24:00", "tz": LONDON},
        {"time": "16:60", "tz": LONDON},
        {"time": "4pm", "tz": LONDON},
        {"time": "16:00:00", "tz": LONDON},
        {"time": "6:00", "tz": LONDON},
        {"time": 1600, "tz": LONDON},
        {"time": " 16:00", "tz": LONDON},
    ],
)
def test_a_malformed_publication_falls_back_to_the_default_and_keeps_the_area(
    publication: Any, caplog: pytest.LogCaptureFixture
) -> None:
    document = stated()
    gb = next(area for area in document["areas"] if area["id"] == "GB-C")
    gb["publication"] = publication
    with caplog.at_level(logging.WARNING):
        catalogue = parse_catalogue(document, version=2)
        again = parse_catalogue(document, version=2)
    assert [entry.id for entry in catalogue.areas] == ["ES-PVPC", "GB-C", "PT", "SE4"]
    assert catalogue.area("GB-C").publication == DEFAULT_PUBLICATION
    assert again.area("GB-C").publication == DEFAULT_PUBLICATION
    # The other areas keep what they state.
    assert catalogue.area("ES-PVPC").publication == PVPC
    logged = [record for record in caplog.records if "GB-C" in record.getMessage() and "publication" in record.getMessage()]
    assert len(logged) == 1, [record.getMessage() for record in caplog.records]


# --- when the waiting text says the prices are due -----------------------------------------------


@pytest.mark.parametrize(
    ("missing_day", "expected"),
    [
        # Great Britain, 16:00 UK plus 45 minutes: 16:45 GMT or BST, whichever the publication day has.
        (date(2026, 3, 29), "2026-03-28T16:45:00+00:00"),  # published Saturday, still GMT
        (date(2026, 3, 30), "2026-03-29T15:45:00+00:00"),  # published Sunday, now BST
        (date(2026, 10, 25), "2026-10-24T15:45:00+00:00"),  # published Saturday, still BST
        (date(2026, 10, 26), "2026-10-25T16:45:00+00:00"),  # published Sunday, now GMT
    ],
)
def test_great_britain_is_due_at_16_45_uk_time_on_both_sides_of_each_clock_change(
    missing_day: date, expected: str
) -> None:
    at = expected_publication_at(missing_day, zone=LONDON, local_time=time(16, 0))
    assert at == utc(expected)
    assert at.astimezone(ZoneInfo(LONDON)).time() == time(16, 45)


@pytest.mark.parametrize(
    ("missing_day", "expected"),
    [
        # Spain's PVPC, 20:15 Madrid plus 45 minutes: 21:00 CET or CEST.
        (date(2026, 3, 29), "2026-03-28T20:00:00+00:00"),  # CET
        (date(2026, 3, 30), "2026-03-29T19:00:00+00:00"),  # CEST
        (date(2026, 10, 25), "2026-10-24T19:00:00+00:00"),  # CEST
        (date(2026, 10, 26), "2026-10-25T20:00:00+00:00"),  # CET
    ],
)
def test_spain_pvpc_is_due_at_21_00_madrid_on_both_sides_of_each_clock_change(missing_day: date, expected: str) -> None:
    assert expected_publication_at(missing_day, zone=MADRID, local_time=time(20, 15)) == utc(expected)


def test_the_gap_a_plan_waits_for_is_the_market_days_and_the_time_the_areas() -> None:
    """A London evening hour is the next Paris file, published at 16:00 UK the day before that file."""
    catalogue = parse_catalogue(stated(), version=2)
    gb = catalogue.area("GB-C")
    pvpc = catalogue.area("ES-PVPC")
    portugal = catalogue.area("PT")
    # 23:00 BST on the 24th is 00:00 on the 25th in Paris: the 25th's file, published on the 24th (BST).
    assert expected_publication(gb, utc("2026-10-24T23:00:00+01:00")) == utc("2026-10-24T16:45:00+01:00")
    # 23:00 GMT on the 25th is the 26th's file, published on the 25th (GMT since the night).
    assert expected_publication(gb, utc("2026-10-25T23:00:00+00:00")) == utc("2026-10-25T16:45:00+00:00")
    # Spain's PVPC across the spring change: Sunday's file is published on Saturday (CET), Monday's on Sunday (CEST).
    assert expected_publication(pvpc, utc("2026-03-29T00:00:00+01:00")) == utc("2026-03-28T21:00:00+01:00")
    assert expected_publication(pvpc, utc("2026-03-30T00:00:00+02:00")) == utc("2026-03-29T21:00:00+02:00")
    # An area stating nothing: 13:00 Brussels plus 45 minutes, as always.
    assert expected_publication(portugal, utc("2026-10-06T00:00:00+02:00")) == utc("2026-10-05T13:45:00+02:00")


def test_the_waiting_line_carries_the_areas_own_time() -> None:
    """The status line names the instant; the card shows it on the area's clock (16:45 in London)."""
    catalogue = parse_catalogue(stated(), version=2)
    at = expected_publication(catalogue.area("GB-C"), utc("2026-10-05T23:00:00+01:00"))
    status = compose_status(
        StatusFacts(
            now=utc("2026-10-05T09:00:00+01:00"),
            has_settings=True,
            planning=PlanningFacts(state="waiting_for_publication", reason="publication_pending", publication_at=at),
        )
    )
    line = next(line for line in status["lines"] if line["code"] == "waiting_for_publication")
    assert line["params"]["publication_at"] == "2026-10-05T15:45:00+00:00"


# --- when the index is polled often --------------------------------------------------------------


@pytest.mark.parametrize(
    ("instant", "inside"),
    [
        # Summer (BST): 15:55-18:00 London is 14:55-17:00 UTC.
        ("2026-10-24T14:54:59+00:00", False),
        ("2026-10-24T14:55:00+00:00", True),
        ("2026-10-24T16:59:59+00:00", True),
        ("2026-10-24T17:00:00+00:00", False),
        # Winter (GMT), the day after the change: the same wall clock, one hour later in UTC.
        ("2026-10-25T15:54:59+00:00", False),
        ("2026-10-25T15:55:00+00:00", True),
        ("2026-10-25T17:59:59+00:00", True),
        ("2026-10-25T18:00:00+00:00", False),
        # The spring change.
        ("2026-03-28T15:55:00+00:00", True),
        ("2026-03-29T17:00:00+00:00", False),
        ("2026-03-29T14:55:00+00:00", True),
        # 13:00 Brussels is not Great Britain's window.
        ("2026-10-24T11:00:00+00:00", False),
    ],
)
def test_great_britains_window_is_15_55_to_18_00_uk_time(instant: str, inside: bool) -> None:
    assert in_publication_window(utc(instant), GB) is inside


@pytest.mark.parametrize(
    ("instant", "inside"),
    [
        # 20:10-22:15 Madrid: CEST is 18:10-20:15 UTC, CET 19:10-21:15 UTC.
        ("2026-10-24T18:09:59+00:00", False),
        ("2026-10-24T18:10:00+00:00", True),
        ("2026-10-24T20:14:59+00:00", True),
        ("2026-10-24T20:15:00+00:00", False),
        ("2026-10-25T19:10:00+00:00", True),
        ("2026-10-25T21:14:59+00:00", True),
        ("2026-10-25T18:10:00+00:00", False),
        ("2026-03-28T19:10:00+00:00", True),
        ("2026-03-29T18:10:00+00:00", True),
        ("2026-03-29T21:00:00+00:00", False),
    ],
)
def test_spain_pvpcs_window_is_20_10_to_22_15_madrid(instant: str, inside: bool) -> None:
    assert in_publication_window(utc(instant), PVPC) is inside


def test_a_window_that_runs_past_midnight_is_still_open_after_it() -> None:
    late = AreaPublication(local_time=time(23, 0), tz=BRUSSELS)
    assert in_publication_window(utc("2026-10-05T23:30:00+02:00"), late) is True
    assert in_publication_window(utc("2026-10-06T00:59:59+02:00"), late) is True
    assert in_publication_window(utc("2026-10-06T01:00:00+02:00"), late) is False


def manager_for(hass: HomeAssistant, transport: StubTransport, clock: Clock) -> PriceRefreshManager:
    repository = PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=StoreDouble())
    return PriceRefreshManager(hass, repository, now=clock, jitter=lambda: 0.5, scheduler=FakeScheduler())


def serve_published_list(transport: StubTransport, listing: dict[str, list[date]]) -> None:
    """The stated list, an index listing only these days, and every fixture day file there is."""
    transport.serve("/v2/areas.json", 200, fixture("areas-v2-publication.json"))
    index = json.loads((V2 / "index-v2.json").read_text("utf-8"))
    index["areas"] = {
        area: {"days": sorted(day.isoformat() for day in days), "res": 30 if area.startswith("GB") else 15}
        for area, days in listing.items()
    }
    transport.serve("/v2/index.json", 200, json.dumps(index))
    for path in V2.glob("*_*.json"):
        area, _, rest = path.stem.partition("_")
        day = date.fromisoformat(rest)
        transport.serve(f"/v1/{area}/{day.year:04d}/{day.month:02d}-{day.day:02d}.json", 200, path.read_text("utf-8"))


async def _poll_after(hass: HomeAssistant, transport: StubTransport, clock: Clock, now: datetime, area: str) -> timedelta:
    clock.now = now
    serve_published_list(transport, {"GB-C": [date(2026, 10, 3), date(2026, 10, 4)], "PT": [date(2026, 10, 4)]})
    manager = manager_for(hass, transport, clock)
    await manager.async_subscribe(owner_id="a", area_id=area)
    await hass.async_block_till_done()
    snapshot = manager.area_snapshot(area)
    assert snapshot is not None and snapshot.waiting_for_tomorrow
    assert snapshot.next_attempt is not None
    delay = snapshot.next_attempt - now
    await manager.async_shutdown()
    return delay


@pytest.mark.parametrize(
    ("now", "area", "seconds"),
    [
        # 14:00 Brussels (13:00 BST): the default window is open, Great Britain's is not yet.
        (datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc), "PT", PUBLICATION_POLL_SECONDS),
        (datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc), "GB-C", HEALTHY_POLL_SECONDS),
        # 16:30 BST: Great Britain's window is open, the default one has closed.
        (datetime(2026, 10, 4, 15, 30, tzinfo=timezone.utc), "GB-C", PUBLICATION_POLL_SECONDS),
        (datetime(2026, 10, 4, 15, 30, tzinfo=timezone.utc), "PT", HEALTHY_POLL_SECONDS),
    ],
)
async def test_the_refresh_polls_often_around_the_subscribed_areas_own_time(
    hass: HomeAssistant, transport: StubTransport, clock: Clock, now: datetime, area: str, seconds: int
) -> None:
    assert await _poll_after(hass, transport, clock, now, area) == timedelta(seconds=seconds)


# --- the planner, end to end ---------------------------------------------------------------------


def _serve_as_v2(transport: StubTransport, publication: Any) -> None:
    """The v1 fixtures served as a v2 relay, SE4 stating `publication` (or nothing, for `None`)."""
    areas = json.loads(fixture("areas.json"))
    areas["v"] = 2
    for area in areas["areas"]:
        area["source"] = {"name": "ENTSO-E Transparency Platform", "url": "https://transparency.entsoe.eu/"}
        if area["id"] == SE4 and publication is not None:
            area["publication"] = publication
    transport.serve("/v2/areas.json", 200, json.dumps(areas))
    status, chunks, _ = transport.routes["/v1/index.json"]
    index = json.loads(b"".join(chunks))
    index["v"] = 2
    transport.serve("/v2/index.json", status, json.dumps(index))


@pytest.mark.parametrize(
    ("publication", "expected"),
    [
        # An artificial 16:00 London for SE4, so only the stated time can explain the answer.
        ({"time": "16:00", "tz": LONDON}, datetime(2026, 9, 22, 15, 45, tzinfo=timezone.utc)),
        # Stated as ENTSO-E's, absent, and malformed: 13:00 Brussels plus 45 minutes.
        ({"time": "13:00", "tz": BRUSSELS}, datetime(2026, 9, 22, 11, 45, tzinfo=timezone.utc)),
        (None, datetime(2026, 9, 22, 11, 45, tzinfo=timezone.utc)),
        ({"time": "16", "tz": LONDON}, datetime(2026, 9, 22, 11, 45, tzinfo=timezone.utc)),
    ],
)
async def test_a_wait_for_tomorrow_says_the_areas_own_time(
    harness: Harness, no_execution: dict[str, Any], publication: Any, expected: datetime
) -> None:
    serve(harness.transport, days=(TODAY,), listed=(TODAY,))
    harness.transport.serve(harness.transport.day_path(SE4, TODAY), 200, cheap_night_day(SE4, TODAY))
    _serve_as_v2(harness.transport, publication)
    controller = await harness.auto(departure=time(8, 0))

    snapshot = controller.snapshot()
    assert harness.transport.call_count("/v2/areas.json") >= 1
    assert snapshot.state == "waiting_for_publication" and snapshot.reason == "publication_pending"
    assert snapshot.publication_at == expected
    assert_nothing_executed(no_execution)
