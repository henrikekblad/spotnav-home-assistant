"""Relay contract v2: the v2 documents with a v1 fallback, and display days cut from market days.

The documents are the relay's own (`tests/fixtures/relay_v2/`, written by the relay's Go writer). The
scenario is the one they were written for: a London evening, 2026-10-04 23:30 BST, already the 5th in
Paris and Madrid, so "today" in London and Lisbon needs two market-day files.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_controller import fiscal_choice_for
from custom_components.spotnav.planning.auto_settings import AreaAutoSettings, AutoSettings, FiscalOverride
from custom_components.spotnav.planning.planner import effective_minor_per_kwh, planning_slots
from custom_components.spotnav.pricing.market_day import (
    compose_display_day,
    market_days_for,
    principal_market_day,
)
from custom_components.spotnav.pricing.price_refresh import PriceRefreshManager
from custom_components.spotnav.pricing.price_repository import PriceRepository
from custom_components.spotnav.pricing.relay_contract import (
    RelayParseError,
    parse_catalogue,
    parse_day,
    parse_index,
)
from custom_components.spotnav.sessions.costing import price_slices, split_energy, spot_intervals

from .relay import BASE_URL, Clock, FakeScheduler, StoreDouble, StubTransport

V2 = Path(__file__).parent / "fixtures" / "relay_v2"
LONDON = "Europe/London"
PARIS = "Europe/Paris"
LISBON = "Europe/Lisbon"
MADRID = "Europe/Madrid"
OCT4 = date(2026, 10, 4)
OCT5 = date(2026, 10, 5)
OCT6 = date(2026, 10, 6)
HALF_HOUR = timedelta(minutes=30)


def raw(name: str) -> str:
    return (V2 / name).read_text(encoding="utf-8")


def doc(name: str) -> Any:
    return json.loads(raw(name))


def gb_file(day: date) -> Any:
    return parse_day(doc(f"GB-C_{day.isoformat()}.json"), area_id="GB-C", day=day)


def serve_v2(transport: StubTransport, *, days: dict[str, list[date]] | None = None) -> None:
    """The relay as deployed: both versions of the list and index, and the fixture day files."""
    transport.serve("/v2/areas.json", 200, raw("areas-v2.json"))
    transport.serve("/v1/areas.json", 200, raw("areas-v1.json"))
    if days is None:
        transport.serve("/v2/index.json", 200, raw("index-v2.json"))
    else:
        index = doc("index-v2.json")
        index["areas"] = {area: {"days": sorted(d.isoformat() for d in listed), "res": 30 if area.startswith("GB") else 15}
                          for area, listed in days.items()}
        transport.serve("/v2/index.json", 200, json.dumps(index))
    transport.serve("/v1/index.json", 200, raw("index-v1.json"))
    for path in V2.glob("*_*.json"):
        area, _, rest = path.stem.partition("_")
        day = date.fromisoformat(rest)
        transport.serve(f"/v1/{area}/{day.year:04d}/{day.month:02d}-{day.day:02d}.json", 200, path.read_text("utf-8"))


def repository_for(hass: HomeAssistant, transport: StubTransport, clock: Clock, store: StoreDouble | None = None) -> PriceRepository:
    return PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=store or StoreDouble())


# --- the documents -------------------------------------------------------------------------------


def test_the_v2_list_carries_market_calendar_included_parts_source_and_an_optional_eic() -> None:
    catalogue = parse_catalogue(doc("areas-v2.json"), version=2)
    assert catalogue.version == 2
    gb = catalogue.area("GB-C")
    assert gb is not None
    assert gb.eic is None
    assert (gb.tz, gb.market_tz) == (LONDON, PARIS)
    assert gb.split_calendar
    assert gb.included == ("vat", "tax", "grid_fee")
    assert gb.source is not None and gb.source.name == "Octopus Energy (Agile)"
    assert gb.source.url == "https://octopus.energy/smart/agile/"
    assert (gb.currency, gb.major_unit, gb.minor_unit) == ("GBP", "£", "p")
    assert gb.vat_percent is None and gb.suggested_tax is None and gb.suggested_grid_fee is None

    portugal = catalogue.area("PT")
    assert portugal is not None and (portugal.tz, portugal.market_tz) == (LISBON, MADRID)
    assert portugal.included == () and portugal.eic == "10YPT-REN------W"
    sweden = catalogue.area("SE4")
    assert sweden is not None and sweden.market_tz == sweden.tz and not sweden.split_calendar


def test_a_v1_list_reads_as_before_and_each_version_is_read_by_its_own_rules() -> None:
    v1 = parse_catalogue(doc("areas-v1.json"))
    portugal = v1.area("PT")
    assert portugal is not None
    # v1 keeps Madrid for Portugal: one zone for both calendars, nothing included, no attribution.
    assert (portugal.tz, portugal.market_tz, portugal.included, portugal.source) == (MADRID, MADRID, (), None)
    assert v1.area("GB-C") is None
    for document, version in ((doc("areas-v2.json"), 1), (doc("areas-v1.json"), 2)):
        with pytest.raises(RelayParseError) as caught:
            parse_catalogue(document, version=version)
        assert caught.value.code == "unsupported_version"


@pytest.mark.parametrize(
    "damage",
    [
        {"included": ["vat", "standing_charge"]},
        {"included": ["vat", "vat"]},
        {"included": "vat"},
        {"source": {"name": "Octopus", "url": "javascript:alert(1)"}},
        {"source": {"name": "", "url": "https://octopus.energy/"}},
        {"source": None},
        {"market_tz": "Mars/Olympus"},
        {"eic": ""},
        {"id": "GB-C-" + "X" * 28},
        {"id": "gb-c"},
    ],
)
def test_an_area_this_client_cannot_use_is_skipped_and_the_rest_kept(damage: dict[str, Any], caplog) -> None:
    document = doc("areas-v2.json")
    entry = document["areas"][0]
    for key, value in damage.items():
        if value is None:
            entry.pop(key)
        else:
            entry[key] = value
    with caplog.at_level("WARNING"):
        catalogue = parse_catalogue(document, version=2)
    assert catalogue.area_ids == ("PT", "SE4")
    assert "Skipping area" in caplog.text


def test_ids_up_to_32_characters_are_areas_and_a_repeat_still_refuses_the_list() -> None:
    document = doc("areas-v2.json")
    long_id = "GB-" + "C" * 29
    document["areas"][0]["id"] = long_id
    assert parse_catalogue(document, version=2).area(long_id) is not None
    document["areas"].append(dict(document["areas"][1]))
    with pytest.raises(RelayParseError) as caught:
        parse_catalogue(document, version=2)
    assert caught.value.code == "duplicate_area"


def test_the_v2_index_lists_market_days_with_half_hours() -> None:
    index = parse_index(doc("index-v2.json"), version=2)
    gb = index.area("GB-C")
    assert gb is not None and gb.resolution_minutes == 30
    assert gb.days == (OCT4, OCT5)
    with pytest.raises(RelayParseError):
        parse_index(doc("index-v2.json"))


def test_a_gb_file_is_a_paris_day_of_half_hours() -> None:
    file = gb_file(OCT4)
    assert file.tz == PARIS
    assert file.resolution_minutes == 30
    assert file.interval_count == 48
    assert file.covers_whole_day()
    # 00:00 in Paris is 23:00 the evening before in London.
    assert file.start_instant == datetime(2026, 10, 3, 22, 0, tzinfo=timezone.utc)
    spring = gb_file(date(2026, 3, 29))
    autumn = gb_file(date(2026, 10, 25))
    assert (spring.interval_count, autumn.interval_count) == (46, 50)
    assert spring.covers_whole_day() and autumn.covers_whole_day()


# --- market days ---------------------------------------------------------------------------------


def test_a_london_day_needs_two_paris_files_and_belongs_to_the_first() -> None:
    assert market_days_for(OCT4, LONDON, PARIS) == (OCT4, OCT5)
    assert principal_market_day(OCT4, LONDON, PARIS) == OCT4
    assert market_days_for(OCT4, LISBON, MADRID) == (OCT4, OCT5)
    assert market_days_for(OCT4, "Europe/Stockholm", "Europe/Stockholm") == (OCT4,)
    # Both clock changes: the UK and the EU change at the same instant, so still two files.
    assert market_days_for(date(2026, 3, 29), LONDON, PARIS) == (date(2026, 3, 29), date(2026, 3, 30))
    assert market_days_for(date(2026, 10, 25), LONDON, PARIS) == (date(2026, 10, 25), date(2026, 10, 26))


def test_a_london_day_is_cut_from_both_files_each_hour_keeping_its_own_rate() -> None:
    first, second = gb_file(OCT4), gb_file(OCT5)
    second_doc = doc("GB-C_2026-10-05.json")
    second_doc["fx"] = {"GBP": 0.9}
    second = parse_day(second_doc, area_id="GB-C", day=OCT5)
    day = compose_display_day("GB-C", OCT4, LONDON, (second, first))
    assert day is not None
    assert (day.day, day.tz, day.resolution_minutes) == (OCT4, LONDON, 30)
    assert day.interval_count == 48
    assert day.start_instant == datetime(2026, 10, 3, 23, 0, tzinfo=timezone.utc)
    assert day.covers_until_instant == datetime(2026, 10, 4, 23, 0, tzinfo=timezone.utc)
    # 46 half-hours of the 4th's file (its first hour is the 3rd in London) and two of the 5th's.
    assert [part.interval_count for part in day.parts] == [46, 2]
    assert day.prices[:46] == first.prices[2:]
    assert day.prices[46:] == second.prices[:2]
    slots = planning_slots((day,), currency="GBP", timezone=LONDON)
    # 30-minute rows become two planning quarters each; the last hour is priced at the 5th's own rate.
    assert len(slots) == 96
    assert slots[-1].local_major_per_kwh == pytest.approx(second.prices[1] * 0.9)
    assert slots[0].local_major_per_kwh == pytest.approx(first.prices[2] * 0.8712)


def test_a_london_day_whose_next_file_is_not_out_yet_simply_ends_at_23() -> None:
    day = compose_display_day("GB-C", OCT4, LONDON, (gb_file(OCT4),))
    assert day is not None and day.parts == ()
    assert day.interval_count == 46
    assert day.covers_until_instant == datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("day", "files", "count"),
    [
        # Spring: London's 23-hour day is 22 hours of the 29th's file (46 half-hours) and one of the 30th's.
        (date(2026, 3, 29), (date(2026, 3, 29),), 44),
        # Autumn: London's 25-hour day, 24 hours of the 25th's file (50) and one of the 26th's.
        (date(2026, 10, 25), (date(2026, 10, 25), date(2026, 10, 26)), 50),
    ],
)
def test_both_clock_changes_cut_by_instant(day: date, files: tuple[date, ...], count: int) -> None:
    composed = compose_display_day("GB-C", day, LONDON, tuple(gb_file(item) for item in files))
    assert composed is not None
    assert composed.interval_count == count
    steps = [b.utc_start - a.utc_start for a, b in zip(composed.intervals, composed.intervals[1:])]
    assert set(steps) == {HALF_HOUR}


# --- the repository: v2 first, v1 behind it ------------------------------------------------------


async def test_the_list_and_index_are_read_as_v2_when_the_relay_has_it(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    serve_v2(transport)
    repository = repository_for(hass, transport, clock)
    catalogue = await repository.async_get_catalogue(refresh=True)
    index = await repository.async_get_index(refresh=True)
    assert catalogue.catalogue is not None and catalogue.catalogue.version == 2
    assert index.index is not None and index.index.version == 2
    assert transport.calls == ["/v2/areas.json", "/v2/index.json"]


@pytest.mark.parametrize("v2", [(404, "not found"), (200, "<html>proxy</html>"), (200, '{"v": 3}')])
async def test_an_old_or_unreadable_v2_falls_back_to_v1_and_the_index_follows(
    hass: HomeAssistant, transport: StubTransport, clock: Clock, v2: tuple[int, str]
) -> None:
    serve_v2(transport)
    transport.serve("/v2/areas.json", *v2)
    repository = repository_for(hass, transport, clock)
    catalogue = await repository.async_get_catalogue(refresh=True)
    index = await repository.async_get_index(refresh=True)
    assert catalogue.state == "ready" and catalogue.catalogue is not None
    assert catalogue.catalogue.version == 1
    assert catalogue.catalogue.area("GB-C") is None
    # The index is the list's version, never the newer one beside it.
    assert index.index is not None and index.index.version == 1
    assert transport.calls == ["/v2/areas.json", "/v1/areas.json", "/v1/index.json"]


async def test_an_unreachable_relay_is_not_taken_for_an_old_one(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    serve_v2(transport)
    transport.serve("/v2/areas.json", 503, "busy")
    repository = repository_for(hass, transport, clock)
    catalogue = await repository.async_get_catalogue(refresh=True)
    assert catalogue.state == "unavailable"
    assert transport.calls == ["/v2/areas.json"]


async def test_a_v1_cache_restores_its_held_days_and_moves_to_v2(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    """A cache written by an older release (v1 list and index) loses nothing it held."""
    clock.now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    fetched = clock.now.isoformat()
    store = StoreDouble(
        {
            "schema": 1,
            "catalogue": {"document": doc("areas-v1.json"), "fetched_at": fetched},
            "index": {"document": doc("index-v1.json"), "fetched_at": fetched},
            "days": {"PT|2026-10-04": {"document": doc("PT_2026-10-04.json"), "fetched_at": fetched}},
            "profiles": {},
        }
    )
    serve_v2(transport)
    repository = repository_for(hass, transport, clock, store)
    await repository.async_restore()
    assert repository.catalogue_snapshot().catalogue.version == 1
    held = repository.day_snapshot("PT", OCT4)
    # v1 Portugal is one Madrid calendar: the held file is the day.
    assert held.document is not None and held.document.interval_count == 96

    await repository.async_get_catalogue(refresh=True)
    assert repository.catalogue_snapshot().catalogue.version == 2
    # The v1 index is not authority beside a v2 list; the next read is v2.
    assert repository.index_snapshot().index is None
    await repository.async_get_index(refresh=True)
    assert repository.index_snapshot().index.version == 2
    assert repository.file_snapshot("PT", OCT4).document is not None
    assert "PT|2026-10-04" in store.payload["days"]


async def test_an_archive_london_day_asks_for_a_shared_file_once(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    serve_v2(transport, days={"GB-C": []})
    repository = repository_for(hass, transport, clock)
    await repository.async_get_catalogue(refresh=True)
    await repository.async_get_index(refresh=True)
    first = await repository.async_get_archive_day("GB-C", OCT4)
    second = await repository.async_get_archive_day("GB-C", OCT5)
    assert first is not None and first.interval_count == 48 and first.tz == LONDON
    # The 5th's file is the end of the 4th in London and most of the 5th: asked for once.
    assert transport.call_count("/v1/GB-C/2026/10-05.json") == 1
    assert second is not None and second.interval_count == 46
    # A day whose principal file the relay does not have is no day.
    assert await repository.async_get_archive_day("GB-C", date(2026, 10, 7)) is None


# --- the manager: market days behind display days ------------------------------------------------


def manager_for(hass: HomeAssistant, repository: PriceRepository, clock: Clock) -> PriceRefreshManager:
    return PriceRefreshManager(hass, repository, now=clock, jitter=lambda: 0.5, scheduler=FakeScheduler())


async def test_a_london_evening_needs_the_next_paris_file(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    # 23:30 BST on the 4th: 00:30 on the 5th in Paris.
    clock.now = datetime(2026, 10, 4, 22, 30, tzinfo=timezone.utc)
    serve_v2(transport)
    repository = repository_for(hass, transport, clock)
    manager = manager_for(hass, repository, clock)
    await manager.async_subscribe(owner_id="a", area_id="GB-C")
    await hass.async_block_till_done()

    snapshot = manager.area_snapshot("GB-C")
    assert snapshot is not None
    assert (snapshot.today, snapshot.tomorrow) == (OCT4, OCT5)
    assert transport.call_count("/v1/GB-C/2026/10-04.json") == 1
    assert transport.call_count("/v1/GB-C/2026/10-05.json") == 1
    today = snapshot.today_document
    assert today is not None and today.tz == LONDON and today.interval_count == 48
    # The current half-hour (23:30 London) is priced, from the 5th's Paris file.
    now = clock.now
    assert any(interval.applies_at(now) for interval in today.intervals)
    assert snapshot.state == "ready" and snapshot.reason == "ready"
    # Tomorrow in London is 23 hours of the 5th's file; the last hour comes with the next publication.
    assert snapshot.tomorrow_state == "ready" and not snapshot.waiting_for_tomorrow
    assert snapshot.tomorrow_document is not None and snapshot.tomorrow_document.interval_count == 46
    assert snapshot.tomorrow_snapshot.is_complete
    await manager.async_shutdown()


async def test_a_london_afternoon_before_publication_waits_for_tomorrow(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    # 14:00 BST on the 4th: the index lists the 3rd and 4th in Paris, not yet the 5th.
    clock.now = datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc)
    serve_v2(transport, days={"GB-C": [date(2026, 10, 3), OCT4], "PT": [OCT4], "SE4": [OCT4]})
    repository = repository_for(hass, transport, clock)
    manager = manager_for(hass, repository, clock)
    await manager.async_subscribe(owner_id="a", area_id="GB-C")
    await hass.async_block_till_done()

    snapshot = manager.area_snapshot("GB-C")
    assert snapshot is not None
    # Today is whole as far as anything is published: it ends at 23:00, which is not a fault.
    assert snapshot.today_state == "ready"
    assert snapshot.today_document is not None and snapshot.today_document.interval_count == 46
    assert snapshot.waiting_for_tomorrow and snapshot.state == "waiting_for_tomorrow"
    # Nothing unpublished is asked for.
    assert transport.call_count("/v1/GB-C/2026/10-05.json") == 0
    assert transport.call_count("/v1/GB-C/2026/10-03.json") == 0
    await manager.async_shutdown()


async def test_lisbon_is_read_on_madrid_market_days(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    clock.now = datetime(2026, 10, 4, 22, 30, tzinfo=timezone.utc)
    serve_v2(transport)
    repository = repository_for(hass, transport, clock)
    manager = manager_for(hass, repository, clock)
    await manager.async_subscribe(owner_id="a", area_id="PT")
    await hass.async_block_till_done()
    snapshot = manager.area_snapshot("PT")
    assert snapshot is not None and snapshot.today == OCT4
    today = snapshot.today_document
    assert today is not None and today.tz == LISBON
    # A Lisbon day is 23 hours of Madrid's 4th and the first hour of Madrid's 5th: 96 quarters.
    assert today.interval_count == 96
    assert [part.day for part in today.parts] == [OCT4, OCT4]
    assert today.start_instant == datetime(2026, 10, 3, 23, 0, tzinfo=timezone.utc)
    assert snapshot.state == "ready"
    await manager.async_shutdown()


async def test_a_v1_relay_keeps_portugal_on_one_madrid_calendar(
    hass: HomeAssistant, transport: StubTransport, clock: Clock
) -> None:
    clock.now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    serve_v2(transport)
    transport.serve("/v2/areas.json", 404, "not found")
    transport.serve("/v2/index.json", 404, "not found")
    repository = repository_for(hass, transport, clock)
    manager = manager_for(hass, repository, clock)
    await manager.async_subscribe(owner_id="a", area_id="PT")
    await hass.async_block_till_done()
    snapshot = manager.area_snapshot("PT")
    assert snapshot is not None
    today = snapshot.today_document
    assert today is not None and today.tz == MADRID and today.parts == ()
    assert transport.call_count("/v1/PT/2026/10-05.json") == 1
    await manager.async_shutdown()


# --- included fiscal parts -----------------------------------------------------------------------


def _all_on(area_id: str) -> AutoSettings:
    stated = FiscalOverride(enabled=True, value=25.0)
    return AutoSettings(
        overrides=(AreaAutoSettings(area_id=area_id, vat=stated, tax=stated, transfer=stated),),
        area_id=area_id,
    )


def test_included_parts_are_locked_off_whatever_is_stored() -> None:
    catalogue = parse_catalogue(doc("areas-v2.json"), version=2)
    gb = catalogue.area("GB-C")
    fiscal = fiscal_choice_for(_all_on("GB-C"), gb)
    assert fiscal is not None
    assert not (fiscal.vat_enabled or fiscal.tax_enabled or fiscal.transfer_enabled)
    # The planner adds nothing: the effective price is the published one.
    assert effective_minor_per_kwh(0.2135, fiscal) == pytest.approx(21.35)
    # An area that includes nothing keeps every stated figure.
    sweden = fiscal_choice_for(_all_on("SE4"), catalogue.area("SE4"))
    assert sweden is not None and sweden.vat_enabled and sweden.tax_enabled and sweden.transfer_enabled


def test_a_session_on_an_all_in_area_costs_the_published_price() -> None:
    catalogue = parse_catalogue(doc("areas-v2.json"), version=2)
    fiscal = fiscal_choice_for(_all_on("GB-C"), catalogue.area("GB-C"))
    day = compose_display_day("GB-C", OCT4, LONDON, (gb_file(OCT4), gb_file(OCT5)))
    assert day is not None
    spot = spot_intervals((day,), "GBP")
    start = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
    slices = split_energy(spot, start, start + timedelta(hours=1), 7.0)
    costing = price_slices(slices, fiscal)
    expected = sum(item.kwh * item.eur_per_kwh * 0.8712 * 100 for item in slices)
    assert costing.priced_kwh == pytest.approx(7.0)
    assert costing.cost_minor == pytest.approx(expected)


def test_the_settings_record_states_what_is_included_and_the_app_is_not_shown_it_unasked() -> None:
    from custom_components.spotnav.api.settings import decode_settings, encode_settings, included_components
    from custom_components.spotnav.api.webhook import _for_app

    catalogue = parse_catalogue(doc("areas-v2.json"), version=2)
    included = included_components(catalogue.area("GB-C"))
    assert included == ("vat", "tax", "transfer")
    assert included_components(catalogue.area("SE4")) == ()
    record = encode_settings(_all_on("GB-C"), None, included)
    assert record["fiscal_included"] == ["vat", "tax", "transfer"]
    # A client echoing the record back is not refused, and nothing of it is stored.
    body = {key: value for key, value in record.items() if key != "revision"}
    assert decode_settings(body).override_for("GB-C").vat.enabled
    # The app 1.0.x decoder refuses an unknown settings key: the webhook withholds it unless asked.
    answer = {"ok": True, "settings": record}
    assert "fiscal_included" not in _for_app(answer, {})["settings"]
    assert _for_app(answer, {"reads": ["fiscal_included"]})["settings"]["fiscal_included"] == [
        "vat",
        "tax",
        "transfer",
    ]


def test_the_dashboard_and_the_market_editor_show_included_parts_and_the_source() -> None:
    from custom_components.spotnav.api.dashboard import serialize_fiscal
    from custom_components.spotnav.api.market import _area

    catalogue = parse_catalogue(doc("areas-v2.json"), version=2)
    gb = catalogue.area("GB-C")
    fiscal = serialize_fiscal(_all_on("GB-C"), gb)
    assert fiscal is not None
    for component in ("vat", "tax", "transfer"):
        assert fiscal[component]["policy"] == "included"
        assert fiscal[component]["effective_value"] is None
    portugal = serialize_fiscal(_all_on("PT"), catalogue.area("PT"))
    assert portugal is not None and portugal["vat"]["policy"] == "manual"
    area = _area(gb)
    assert area["included"] == ["vat", "tax", "transfer"]
    assert area["market_timezone"] == PARIS and area["timezone"] == LONDON
    assert area["source"] == {"name": "Octopus Energy (Agile)", "url": "https://octopus.energy/smart/agile/"}


async def test_the_fiscal_entities_of_an_included_part_are_unavailable(hass: HomeAssistant, offline_relay) -> None:
    from homeassistant.const import STATE_UNAVAILABLE

    from .world import entity_id, go_auto, setup_charger

    serve_v2(offline_relay)
    entry = await setup_charger(hass)
    await go_auto(hass, entry.entry_id, area_id="GB-C")
    for platform, key in (
        ("select", "fiscal_vat_policy"),
        ("select", "fiscal_tax_policy"),
        ("select", "fiscal_transfer_policy"),
        ("number", "vat_rate"),
        ("number", "energy_tax"),
        ("number", "transfer_fee"),
    ):
        assert hass.states.get(entity_id(hass, entry.entry_id, key, platform)).state == STATE_UNAVAILABLE
    await go_auto(hass, entry.entry_id, area_id="SE4")
    assert hass.states.get(entity_id(hass, entry.entry_id, "fiscal_vat_policy", "select")).state != STATE_UNAVAILABLE
