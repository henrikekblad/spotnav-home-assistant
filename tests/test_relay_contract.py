"""The relay contract: every fixture, and every rule the parsers enforce.

These are pure tests — no `hass`, no session, no clock — because the contract is a
function of a document. The fixtures they read are checked in under
`tests/fixtures/relay/`, were hand-reviewed, and carry no credential: see that
directory's README for what each one is and which contract revision it
represents.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.pricing.relay_contract import (
    RELAY_PRICE_UNIT,
    SUPPORTED_VERSION,
    PriceInterval,
    RelayParseError,
    loads_document,
    parse_catalogue,
    parse_day,
    parse_index,
    validate_intervals,
)

FIXTURES = Path(__file__).parent / "fixtures" / "relay"

SE4 = "SE4"
STOCKHOLM = ZoneInfo("Europe/Stockholm")


def document(name: str):
    """Read a fixture through the production JSON reader, not `json.load` directly.

    That distinction is the point for the two files carrying `NaN`/`Infinity`:
    `json.loads` accepts them, the contract does not, and a test that bypassed the
    reader would prove nothing about what a client does with the same body.
    """
    return loads_document((FIXTURES / name).read_text(encoding="utf-8"))


def day_at(name: str, area_id: str = SE4, day: date | None = None):
    return parse_day(document(name), area_id=area_id, day=day or date(2026, 9, 22))


def rejection(name: str, code: str, area_id: str = SE4, day: date = date(2026, 9, 22)) -> RelayParseError:
    with pytest.raises(RelayParseError) as caught:
        parse_day(document(name), area_id=area_id, day=day)
    assert caught.value.code == code, f"{name} failed with {caught.value.code}: {caught.value}"
    return caught.value


def test_catalogue_carries_four_money_identities_without_inferring_any():
    catalogue = parse_catalogue(document("areas.json"))

    assert catalogue.version == SUPPORTED_VERSION
    assert catalogue.area_ids == ("DE-LU", "DK1", "NO1", "SE4")

    se4 = catalogue.area("SE4")
    assert se4 is not None
    # Three separate facts, kept separate: the ISO code is the identity, and the
    # labels are labels -- `kr` is deliberately not unique.
    assert (se4.currency, se4.major_unit, se4.minor_unit) == ("SEK", "kr", "öre")
    assert (se4.tz, se4.countries, se4.eic) == ("Europe/Stockholm", ("SE",), "10Y1001A1001A47J")

    eur = catalogue.area("DE-LU")
    nok = catalogue.area("NO1")
    dkk = catalogue.area("DK1")
    assert eur is not None and nok is not None and dkk is not None
    assert (eur.currency, eur.major_unit, eur.minor_unit) == ("EUR", "€", "cent")
    # One label, three identities: a client that read the label as an identity
    # would conflate them.
    assert {nok.major_unit, dkk.major_unit, se4.major_unit} == {"kr"}
    assert {nok.currency, dkk.currency, se4.currency} == {"NOK", "DKK", "SEK"}


def test_catalogue_keeps_present_zero_apart_from_absent():
    catalogue = parse_catalogue(document("areas.json"))

    se4 = catalogue.area("SE4")
    oslo = catalogue.area("NO1")
    dkk = catalogue.area("DK1")
    eur = catalogue.area("DE-LU")
    assert se4 is not None and oslo is not None and dkk is not None and eur is not None

    # Present and non-zero, present and zero, and absent: three states, not two.
    assert (se4.suggested_tax, se4.suggested_grid_fee) == (36.0, 30.0)
    assert (oslo.suggested_tax, oslo.suggested_grid_fee) == (7.13, 0.0)
    assert oslo.suggested_grid_fee == 0.0
    assert (dkk.suggested_tax, dkk.suggested_grid_fee) == (0.0, None)
    assert (eur.vat_percent, eur.suggested_tax, eur.suggested_grid_fee) == (None, None, None)
    # An area with a published VAT of zero is not an area with no VAT figure.
    assert oslo.vat_percent == 25.0


def test_catalogue_rejects_a_repeated_area_id_and_another_contract_version():
    source = document("areas.json")
    twice = json.loads(json.dumps(source))
    twice["areas"].append(dict(twice["areas"][0]))
    with pytest.raises(RelayParseError) as caught:
        parse_catalogue(twice)
    assert caught.value.code == "duplicate_area"

    other = json.loads(json.dumps(source))
    other["v"] = 2
    with pytest.raises(RelayParseError) as versioned:
        parse_catalogue(other)
    assert versioned.value.code == "unsupported_version"


def test_catalogue_skips_an_invalid_area_and_keeps_the_rest(caplog):
    broken = json.loads(json.dumps(document("areas.json")))
    broken["areas"].insert(1, {"id": "BAD", "countries": []})
    broken["areas"].insert(0, "not an object")
    broken["areas"][3]["tz"] = "Mars/Olympus"
    skipped_id = broken["areas"][3]["id"]
    with caplog.at_level("WARNING"):
        catalogue = parse_catalogue(broken)
    assert "BAD" not in catalogue.area_ids
    assert skipped_id not in catalogue.area_ids
    assert catalogue.area_ids == ("DE-LU", "NO1", "SE4")
    assert "Skipping area" in caplog.text


def test_catalogue_keeps_a_saved_area_when_another_entry_is_damaged():
    broken = json.loads(json.dumps(document("areas.json")))
    broken["areas"].append({"id": "TOOLONG-AREA-IDENTIFIER-1234567890", "name": 5})
    catalogue = parse_catalogue(broken)
    assert catalogue.area("SE4") is not None
    assert len(catalogue.areas) == 4


def test_catalogue_still_refuses_a_repeated_id_among_valid_entries():
    twice = json.loads(json.dumps(document("areas.json")))
    twice["areas"].insert(0, {"id": "X"})
    twice["areas"].append(dict(twice["areas"][1]))
    with pytest.raises(RelayParseError) as caught:
        parse_catalogue(twice)
    assert caught.value.code == "duplicate_area"


def test_index_lists_today_and_tomorrow_and_omits_an_authoritative_absence():
    index = parse_index(document("index.json"))

    assert index.areas_rev == "916f2f8e05fd"
    assert index.res_default == 60

    se4 = index.area("SE4")
    assert se4 is not None
    assert se4.days == (date(2026, 9, 21), date(2026, 9, 22))
    assert se4.resolution_minutes == 15
    assert se4.lists(date(2026, 9, 22)) is True

    listed = parse_index(document("index_missing_tomorrow.json")).area("SE4")
    assert listed is not None
    # "Not listed" is an answer: this is what stops a client asking for a day the
    # relay has said it does not have.
    assert listed.lists(date(2026, 9, 22)) is False
    assert listed.lists(date(2026, 9, 21)) is True


def test_index_skips_a_damaged_area_and_keeps_the_rest():
    for name in ("index_days_unsorted.json", "index_days_duplicate.json"):
        full = document(name)
        damaged = [key for key in full["areas"]]
        parsed = parse_index(full)
        # The damaged entries are dropped one by one; nothing is read hopefully.
        assert len(parsed.areas) < len(damaged)
        assert all(entry.days == tuple(sorted(set(entry.days))) for entry in parsed.areas)

    with pytest.raises(RelayParseError) as versioned:
        parse_index(document("index_bad_version.json"))
    assert versioned.value.code == "unsupported_version"

    # A missing per-area `res` is legitimate: `res_default` covers the area.
    assert parse_index(document("index_no_res.json")).area("SE4").resolution_minutes is None

    broken = document("index.json")
    broken["areas"]["SE4"]["res"] = 30
    kept = parse_index(broken)
    assert kept.area("SE4") is None
    assert kept.area("NO1") is not None

    # A damaged entry that is not even an object is skipped the same way.
    odd = document("index.json")
    odd["areas"]["XX1"] = "nope"
    assert parse_index(odd).area("SE4") is not None


def test_a_96_quarter_day_is_preserved_interval_by_interval():
    parsed = day_at("day_SE4_2026-09-22_96.json")

    assert parsed.area_id == SE4
    assert parsed.day == date(2026, 9, 22)
    assert parsed.tz == "Europe/Stockholm"
    assert parsed.resolution_minutes == 15
    assert parsed.unit == RELAY_PRICE_UNIT
    assert parsed.interval_count == 96
    assert parsed.covers_whole_day() is True

    # The relay's own words about provenance, kept beside the prices.
    assert parsed.start.isoformat() == "2026-09-22T00:00:00+02:00"
    assert parsed.fx_rate("SEK") == 11.275
    assert parsed.fx_date == date(2026, 9, 21)
    assert parsed.fx_src == "ECB"
    assert parsed.src == "entsoe.eu"
    assert parsed.published is not None and parsed.published.year == 2026
    assert parsed.retrieved is not None

    # Positional prices and derived intervals describe the same facts, and the
    # intervals step in absolute time: the last one ends at local midnight.
    assert len(parsed.prices) == len(parsed.intervals)
    assert parsed.prices[0] == parsed.intervals[0].eur_per_kwh
    assert parsed.intervals[0].start == parsed.start
    assert parsed.intervals[1].start == parsed.intervals[0].end
    assert parsed.covers_until == parsed.intervals[-1].end
    # Half-open intervals: every instant belongs to exactly one of them.
    first = parsed.intervals[0]
    assert first.applies_at(first.start) is True
    assert first.applies_at(first.end) is False
    assert first.applies_at(first.end - timedelta(seconds=1)) is True
    assert parsed.covers_until.astimezone(STOCKHOLM).date() == date(2026, 9, 23)
    assert all(interval.duration == timedelta(minutes=15) for interval in parsed.intervals)


def test_every_kind_of_local_day_is_accepted_with_its_own_length():
    spring = day_at("day_SE4_2026-03-29_92.json", day=date(2026, 3, 29))
    autumn = day_at("day_SE4_2025-10-26_100.json", day=date(2025, 10, 26))
    hourly = day_at("day_SE4_2025-09-30_hourly.json", day=date(2025, 9, 30))

    # 23 hours, 25 hours, and a market that only ever published hours: three
    # lengths, none of them 96, all complete.
    assert spring.interval_count == 92 and spring.covers_whole_day() is True
    assert autumn.interval_count == 100 and autumn.covers_whole_day() is True
    assert hourly.interval_count == 24 and hourly.covers_whole_day() is True
    assert hourly.resolution_minutes == 60

    # Absolute-time stepping, not wall-clock: the spring day really is 23 hours of
    # instants and the autumn one 25, and the difference is taken by instant --
    # subtracting two local values that share a ZoneInfo would be a wall-clock
    # subtraction and would answer differently on exactly these two days.
    assert spring.covers_until_instant - spring.start_instant == timedelta(hours=23)
    assert autumn.covers_until_instant - autumn.start_instant == timedelta(hours=25)
    assert spring.start == spring.start_instant.astimezone(STOCKHOLM)

    # Every interval is a quarter of elapsed time, on all three kinds of day.
    for document in (spring, autumn, hourly):
        assert all(interval.duration == timedelta(minutes=document.resolution_minutes) for interval in document.intervals)


def test_the_dst_transitions_are_honest_in_the_raw_interval_values():
    """The endpoints themselves, not values a test converted before looking."""
    spring = day_at("day_SE4_2026-03-29_92.json", day=date(2026, 3, 29))
    autumn = day_at("day_SE4_2025-10-26_100.json", day=date(2025, 10, 26))

    # Spring: 02:00-02:59 does not exist locally, so no interval *starts* there.
    # The last interval before the gap reads 01:45+01:00 and the next reads
    # 03:00+02:00 -- the hour is skipped, not invented.
    assert 2 not in {interval.start.hour for interval in spring.intervals}
    before = next(i for i in spring.intervals if i.start.hour == 1 and i.start.minute == 45)
    after = next(i for i in spring.intervals if i.start.hour == 3 and i.start.minute == 0)
    assert before.start.isoformat() == "2026-03-29T01:45:00+01:00"
    # Its end instant is *already* 03:00 in the new offset: the same instant, which
    # is the whole reason the local pair alone is not enough to compare.
    assert before.end.isoformat() == "2026-03-29T03:00:00+02:00"
    assert after.start.isoformat() == "2026-03-29T03:00:00+02:00"
    assert after.utc_start - before.utc_end == timedelta(0)
    assert after.utc_start - before.utc_start == timedelta(minutes=15)

    # Autumn: 02:00-02:59 happens twice, and both passes are present with distinct
    # offsets and folds -- eight quarter starts, four of each.
    in_hour_two = [i for i in autumn.intervals if i.start.hour == 2]
    assert len(in_hour_two) == 8
    assert [i.start.minute for i in in_hour_two[:4]] == [0, 15, 30, 45]
    assert [i.start.minute for i in in_hour_two[4:]] == [0, 15, 30, 45]
    assert {i.start.utcoffset() for i in in_hour_two[:4]} == {timedelta(hours=2)}
    assert {i.start.utcoffset() for i in in_hour_two[4:]} == {timedelta(hours=1)}
    assert {i.start.fold for i in in_hour_two[:4]} == {0}
    assert {i.start.fold for i in in_hour_two[4:]} == {1}
    # The local clock goes backwards across that boundary while the instants do not.
    assert in_hour_two[3].start.isoformat() == "2025-10-26T02:45:00+02:00"
    assert in_hour_two[4].start.isoformat() == "2025-10-26T02:00:00+01:00"
    assert in_hour_two[4].utc_start == in_hour_two[3].utc_end
    assert all(
        later.utc_start == earlier.utc_end for earlier, later in itertools.pairwise(autumn.intervals)
    )


def test_applies_at_picks_one_of_the_two_repeated_hours():
    autumn = day_at("day_SE4_2025-10-26_100.json", day=date(2025, 10, 26))
    summer = next(i for i in autumn.intervals if i.start.isoformat() == "2025-10-26T02:15:00+02:00")
    winter = next(i for i in autumn.intervals if i.start.isoformat() == "2025-10-26T02:15:00+01:00")

    # The two are an hour apart as instants, whatever their wall clocks say.
    assert winter.utc_start - summer.utc_start == timedelta(hours=1)
    assert summer.applies_at(summer.start) is True
    assert summer.applies_at(winter.start) is False
    assert winter.applies_at(winter.start) is True
    assert winter.applies_at(summer.start) is False
    # Half-open at both ends, and the boundary instant belongs to the next one.
    assert summer.applies_at(summer.utc_start) is True
    assert summer.applies_at(summer.utc_end - timedelta(microseconds=1)) is True
    assert summer.applies_at(summer.utc_end) is False
    # An hour of local subtraction would put this *inside* the summer interval, and
    # this is precisely why nothing here subtracts local values: 02:15 winter time
    # is 01:15 UTC, an hour before summer's 02:15 -- which is 00:15 UTC.
    assert summer.applies_at(winter.start) is False
    assert winter.utc_start - summer.utc_start == timedelta(hours=1)
    # Two aware datetimes that share one ZoneInfo subtract as *wall clocks*: this
    # answers zero for two instants an hour apart. Local arithmetic is a trap, and
    # the instants above are the answer to the question callers actually have.
    assert winter.start - summer.start == timedelta(0)
    assert winter.start.utcoffset() - summer.start.utcoffset() == timedelta(hours=-1)
    # An instant in UTC means the same thing as the same instant expressed locally.
    assert summer.applies_at(summer.utc_start) is True
    assert winter.applies_at(summer.utc_start + timedelta(minutes=5)) is False
    with pytest.raises(ValueError):
        summer.applies_at(datetime(2025, 10, 26, 2, 15))


def test_negative_and_zero_prices_are_prices():
    negative = day_at("day_SE4_2026-05-24_negative.json", day=date(2026, 5, 24))

    assert min(negative.prices) < 0
    assert all(interval.eur_per_kwh == price for interval, price in zip(negative.intervals, negative.prices, strict=True))

    with_zero = day_at("day_SE4_2026-09-22_96.json")
    zeroed = json.loads(json.dumps(document("day_SE4_2026-09-22_96.json")))
    zeroed["prices"][0] = 0
    zeroed["prices"][1] = -0.0
    reparsed = parse_day(zeroed, area_id=SE4, day=date(2026, 9, 22))
    assert reparsed.prices[0] == 0.0 and reparsed.prices[1] == 0.0
    assert with_zero.prices[0] != 0.0


def test_an_incomplete_current_day_is_accepted_and_says_how_far_it_goes():
    partial = day_at("day_SE4_2026-09-21_incomplete.json", day=date(2026, 9, 21))

    assert partial.interval_count == 48
    assert partial.covers_whole_day() is False
    assert partial.covers_until.astimezone(STOCKHOLM).isoformat() == "2026-09-21T12:00:00+02:00"


def test_every_malformed_fixture_is_refused_with_its_own_code():
    """One field wrong in each fixture, and the code that says which rule broke."""
    expected = {
        "day_SE4_bad_version.json": "unsupported_version",
        "day_SE4_bad_area.json": "area_mismatch",
        "day_SE4_bad_date.json": "date_mismatch",
        "day_SE4_bad_unit.json": "unit_mismatch",
        "day_SE4_bad_currency.json": "unit_mismatch",
        "day_SE4_bad_res.json": "invalid_resolution",
        "day_SE4_bad_res_zero.json": "invalid_resolution",
        "day_SE4_bad_start_offset.json": "invalid_coverage",
        "day_SE4_bad_timestamp.json": "invalid_timestamp",
        "day_SE4_bad_price_bool.json": "invalid_number",
        "day_SE4_bad_price_text.json": "invalid_number",
        "day_SE4_bad_price_nan.json": "invalid_number",
        "day_SE4_bad_price_infinite.json": "invalid_number",
        "day_SE4_bad_fx_rate.json": "invalid_number",
        "day_SE4_bad_fx_date.json": "invalid_field",
        "day_SE4_bad_published.json": "invalid_timestamp",
    }
    for name, code in expected.items():
        rejection(name, code)


def test_a_document_is_refused_when_it_is_not_the_one_that_was_asked_for():
    # The same document, requested as another area or another day: the prices
    # would be another market's or another day's, so identity is checked and not
    # trusted.
    with pytest.raises(RelayParseError) as area:
        day_at("day_SE4_2026-09-22_96.json", area_id="NO1")
    assert area.value.code == "area_mismatch"
    with pytest.raises(RelayParseError) as when:
        day_at("day_SE4_2026-09-22_96.json", day=date(2026, 9, 21))
    assert when.value.code == "date_mismatch"


def test_a_day_that_would_run_past_its_own_midnight_is_refused():
    # 96 quarters is right for an ordinary day and one hour too many for a
    # 23-hour one: the count is never assumed, the coverage is what decides.
    spring = document("day_SE4_2026-03-29_92.json")
    spring["prices"] = spring["prices"] + spring["prices"][:4]
    with pytest.raises(RelayParseError) as caught:
        parse_day(spring, area_id=SE4, day=date(2026, 3, 29))
    assert caught.value.code == "invalid_coverage"

    # And a day whose array is one interval long is not a day at all.
    short = document("day_SE4_2026-09-22_96.json")
    short["prices"] = short["prices"][:1]
    with pytest.raises(RelayParseError) as single:
        parse_day(short, area_id=SE4, day=date(2026, 9, 22))
    assert single.value.code == "invalid_coverage"


def test_intervals_are_validated_as_geometry_whoever_built_them():
    """The rules a positional wire format cannot express, tested on the model itself.

    A duplicate or an inversion cannot be written on the wire (see the fixtures
    README): positions are derived, so this proves the validators themselves, the
    ones a restored snapshot or a future estimate will have to satisfy too.
    """
    # Built on absolute instants, which is the only way to build one at all: the
    # local half is derived, so the two can never disagree.
    base = datetime(2026, 9, 22, tzinfo=timezone.utc)

    def interval(minutes: int, length: int = 15) -> PriceInterval:
        start = base + timedelta(minutes=minutes)
        return PriceInterval.from_instants(start, start + timedelta(minutes=length), zone=STOCKHOLM, eur_per_kwh=0.1)

    validate_intervals((interval(0), interval(15), interval(30)), "a test day")

    with pytest.raises(RelayParseError) as duplicate:
        validate_intervals((interval(0), interval(0, 15)), "a test day")
    assert duplicate.value.code == "duplicate_interval"

    with pytest.raises(RelayParseError) as inverted:
        validate_intervals((interval(15, 15), interval(0, 15)), "a test day")
    assert inverted.value.code == "interval_order"

    with pytest.raises(RelayParseError) as gap:
        validate_intervals((interval(0), interval(20)), "a test day")
    assert gap.value.code == "interval_gap"

    with pytest.raises(RelayParseError) as duration:
        validate_intervals((interval(0, 0), interval(15)), "a test day")
    assert duration.value.code == "interval_duration"

    # Two intervals that share a start but not an end are duplicates too, and the
    # code says so rather than reporting an ordering symptom.
    with pytest.raises(RelayParseError) as shared_start:
        validate_intervals(
            (
                PriceInterval.from_instants(base, base + timedelta(minutes=30), zone=STOCKHOLM, eur_per_kwh=0.1),
                PriceInterval.from_instants(base, base + timedelta(minutes=15), zone=STOCKHOLM, eur_per_kwh=0.2),
            ),
            "a test day",
        )
    assert shared_start.value.code == "duplicate_interval"


def test_validation_accepts_the_autumn_fallback_and_still_refuses_a_real_reversal():
    """A backwards local clock is legal once a year; a backwards instant never is."""
    autumn = day_at("day_SE4_2025-10-26_100.json", day=date(2025, 10, 26))

    # Accepted: the genuine 25-hour day, whose local times read 02:45 then 02:00.
    validate_intervals(autumn.intervals, "the autumn day")
    # Once, exactly: 02:45+02:00 is followed by 02:00+01:00, and after that the
    # local clock runs forward again. Every adjacent pair of *instants* stays
    # contiguous, which is what the validator checked.
    locals_go_backwards = [
        (earlier.start.isoformat(), later.start.isoformat())
        for earlier, later in itertools.pairwise(autumn.intervals)
        if later.start < earlier.start
    ]
    assert locals_go_backwards == [("2025-10-26T02:45:00+02:00", "2025-10-26T02:00:00+01:00")]

    # Refused: the same local walls with the instants actually reversed.
    summer = next(i for i in autumn.intervals if i.start.isoformat() == "2025-10-26T02:45:00+02:00")
    winter = next(i for i in autumn.intervals if i.start.isoformat() == "2025-10-26T02:00:00+01:00")
    with pytest.raises(RelayParseError) as reversed_pair:
        validate_intervals((winter, summer), "a hand-built pair")
    assert reversed_pair.value.code == "interval_order"


def test_a_body_that_is_not_json_is_refused_as_such():
    # A proxy's error page, which is the shape this actually arrives in.
    with pytest.raises(RelayParseError) as html:
        loads_document("<!doctype html><html><body>502 Bad Gateway</body></html>")
    assert html.value.code == "not_json"

    # `json.loads` would read all three of these into floats; the contract will not.
    for body in ('{"v": 1, "prices": [NaN]}', '{"v": 1, "prices": [Infinity]}', '{"v": 1, "prices": [-Infinity]}'):
        with pytest.raises(RelayParseError) as constant:
            loads_document(body)
        assert constant.value.code == "invalid_number"

    with pytest.raises(RelayParseError) as not_object:
        parse_index([1, 2, 3])
    assert not_object.value.code == "not_an_object"


def test_provenance_is_optional_only_where_the_contract_says_so():
    source = document("day_SE4_2026-09-22_96.json")

    absent = json.loads(json.dumps(source))
    absent["published"] = None
    absent.pop("fx")
    parsed = parse_day(absent, area_id=SE4, day=date(2026, 9, 22))
    # The platform does not always state a creation time, and a day may have no
    # rate close in time to it: both are absences, not defects.
    assert parsed.published is None
    assert dict(parsed.fx) == {} and parsed.fx_rate("SEK") is None

    for missing in ("v", "area", "date", "tz", "start", "res", "unit", "prices"):
        broken = json.loads(json.dumps(source))
        broken.pop(missing)
        with pytest.raises(RelayParseError) as caught:
            parse_day(broken, area_id=SE4, day=date(2026, 9, 22))
        assert caught.value.code == "missing_field", f"removing {missing!r} was not reported as missing"


def test_the_models_are_immutable_rather_than_merely_unmodified():
    """A snapshot handed to two consumers cannot be edited by one of them."""
    parsed = day_at("day_SE4_2026-09-22_96.json")

    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed.prices = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed.intervals[0].eur_per_kwh = 0.0  # type: ignore[misc]
    # The rate table is a read-only view, not a dict that happens to be private.
    with pytest.raises(TypeError):
        parsed.fx["SEK"] = 1.0  # type: ignore[index]

    catalogue = parse_catalogue(document("areas.json"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        catalogue.areas = ()  # type: ignore[misc]
