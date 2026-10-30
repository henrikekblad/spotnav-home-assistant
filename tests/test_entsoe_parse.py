"""The ENTSO-E parser: a port of the relay's parser tests (`entsoe_test.go`, `entsoe_a03_test.go`,
`entsoe_mixed_test.go`, `entsoe_sequence_test.go`, `entsoe_at_ie_test.go`), case for case, on the relay's own
fixtures (`tests/fixtures/entsoe/`).

One difference is deliberate. The relay reads every zone by its own local day; Home Assistant asks for the
**CET market day** (an area's `cet_market_tz`), the day Nord Pool answers too. For the zones on Central
European time the two are the same day. For Eastern European zones (FI, EE, LV, LT, BG, GR, RO) the cases that
are about the relay's local day (the 2025-10-01 day across the 15-minute switch, the FI capture read as a
Helsinki day) run on the local calendar (`local_area`), and a CET twin of each says what Home Assistant gets.
"""

from __future__ import annotations

import re
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.pricing.entsoe_parse import (
    day_document_fields,
    EntsoeArea,
    EntsoeNoData,
    EntsoeParseError,
    parse_day,
    SOURCE_NAME,
)

from .entsoe_helpers import (
    a03_document,
    area,
    bundled_ids,
    AT_EIC,
    d,
    DAY_END,
    DAY_START,
    DE_LU_EIC,
    entsoe_body,
    entsoe_doc,
    FI_EIC,
    fixture,
    half_hours,
    hourly,
    IE_EIC,
    IE_SEM,
    kwh,
    local_area,
    midnight,
    mixed_doc,
    NL_EIC,
    own_zone,
    partial,
    quarter_series,
    same,
    Series,
    starts,
    utc,
    with_sequence,
)


def parse(body: str, area_id: str | EntsoeArea, day: str):
    target = area(area_id) if isinstance(area_id, str) else area_id
    return parse_day(body, target, d(day))


def refuse(body: str, area_id: str | EntsoeArea, day: str) -> str:
    with pytest.raises(EntsoeParseError) as caught:
        parse(body, area_id, day)
    return str(caught.value)


def no_leak(message: str, *extra: str) -> None:
    for forbidden in ("http", "securityToken", *extra):
        assert forbidden not in message, f"{message!r} carries {forbidden!r}"


# --- entsoe_test.go ------------------------------------------------------------------------------------------

#: Every zone the relay's table names, by the CET market day: the Central European zones on their own clock,
#: Portugal on Madrid's, and the Eastern European ones on Brussels'. Clock changes are the same instants
#: everywhere in the EU, so spring has 23 hours and autumn 25.
EVERY_ZONE_IDS = (
    "DE-LU", "PL", "EE", "LV", "LT", "ES", "PT", "BE", "FR", "CH", "CZ", "HR", "HU", "SI", "SK",
    "IT-NORTH", "IT-CNORTH", "IT-CSOUTH", "IT-SOUTH", "IT-CALABRIA", "IT-SICILY", "IT-SARDINIA",
    "BG", "GR", "RO", "FI", "AT", "NL", "SE3",
)  # fmt: skip
CET_DAYS = (
    ("2026-09-19", "202609182200", "202609192200"),
    ("2026-03-29", "202603282300", "202603292200"),
    ("2025-10-26", "202510252200", "202510262300"),
)
EVERY_ZONE = [(area_id, *row) for area_id in EVERY_ZONE_IDS for row in CET_DAYS]


def stamp(compact: str) -> str:
    """202609182200 in the platform's own spelling, 2026-09-18T22:00Z."""
    return f"{compact[0:4]}-{compact[4:6]}-{compact[6:8]}T{compact[8:10]}:{compact[10:12]}Z"


@pytest.mark.parametrize(("area_id", "day", "start", "end"), EVERY_ZONE)
def test_every_zone_parses_its_own_day(area_id: str, day: str, start: str, end: str) -> None:
    target = area(area_id)
    hours = int((utc(stamp(end)) - utc(stamp(start))).total_seconds() // 3600)
    body = entsoe_body(target.eic, stamp(start), stamp(end), "PT60M", [f"{40 + i}.50" for i in range(hours)])
    if target.sequence:
        # A zone that publishes under a sequence has documents that carry it.
        body = with_sequence(body, target.sequence)
    parsed = parse_day(body, target, d(day))
    assert len(parsed.prices_eur_mwh) == hours
    assert parsed.source == "entsoe.eu"
    assert parsed.start == midnight(target, day)
    same(kwh(parsed)[0], 0.0405, "first price")


def test_a_normal_day() -> None:
    parsed = parse(fixture("synthetic_a44_60m.xml"), "DE-LU", "2026-09-19")
    assert len(parsed.prices_eur_mwh) == 24
    for index, euro in ((0, 0.09842), (11, 0.09015), (12, 0.08870), (23, 0.09340)):
        same(kwh(parsed)[index], euro, f"row {index}")
    # The second period restarts at position 1 and lands after the twelfth hour, under its own start.
    assert parsed.start == midnight(area("DE-LU"), "2026-09-19")
    assert starts(parsed)[12] - parsed.start == timedelta(hours=12)
    assert parsed.source == "entsoe.eu"
    assert parsed.published == utc("2026-09-18T12:31Z")
    assert parsed.area_id == "DE-LU" and parsed.market_day == d("2026-09-19")


def test_clock_change_days_are_twenty_three_and_twenty_five_hours() -> None:
    spring = parse(fixture("synthetic_a44_dst_spring.xml"), "DE-LU", "2026-03-29")
    autumn = parse(fixture("synthetic_a44_dst_autumn.xml"), "DE-LU", "2025-10-26")
    assert len(spring.prices_eur_mwh) == 23
    assert len(autumn.prices_eur_mwh) == 25
    berlin = ZoneInfo("Europe/Berlin")
    for held, day, span, twos in ((spring, "2026-03-29", timedelta(hours=22), 0), (autumn, "2025-10-26", timedelta(hours=24), 2)):
        rows = starts(held)
        assert rows[0] == midnight(area("DE-LU"), day)
        assert rows[-1] - rows[0] == span
        # The wall clock is the proof: on the autumn day local 02:00 happens twice.
        assert sum(1 for at in rows if at.astimezone(berlin).hour == 2) == twos


def test_a_quarter_hourly_day_is_ninety_six_rows() -> None:
    parsed = parse(fixture("synthetic_a44_15m.xml"), "DE-LU", "2026-09-19")
    assert len(parsed.prices_eur_mwh) == 96
    assert parsed.resolution_minutes == 15
    same(kwh(parsed)[0], 0.09, "row 0")


def test_negative_prices_survive_the_conversion() -> None:
    parsed = parse(fixture("synthetic_a44_60m_negative.xml"), "PL", "2026-09-19")
    for index, euro in ((0, 0.04520), (8, -0.00125), (9, -0.00314)):
        same(kwh(parsed)[index], euro, f"row {index}")


def test_the_no_data_acknowledgement_is_missing_not_a_day() -> None:
    with pytest.raises(EntsoeNoData):
        parse(fixture("synthetic_ack_999_no_data.xml"), "DE-LU", "2026-09-19")


@pytest.mark.parametrize("name", ["synthetic_ack_999_auth.xml", "synthetic_ack_999_unknown.xml"])
def test_any_other_acknowledgement_is_never_no_data(name: str) -> None:
    # A body alone: only a recognized no-data answer is "not published"; a credential complaint is not a day.
    message = refuse(fixture(name), "DE-LU", "2026-09-19")
    assert "not an A44 document" in message and "securitytoken" not in message


@pytest.mark.parametrize("spelling", ["MWh", "MWH", "mwh"])
def test_the_measure_unit_is_read_case_insensitively(spelling: str) -> None:
    body = fixture("synthetic_a44_60m.xml").replace(
        "<price_Measure_Unit.name>MWh</price_Measure_Unit.name>",
        f"<price_Measure_Unit.name>{spelling}</price_Measure_Unit.name>",
        1,
    )
    assert len(parse(body, "DE-LU", "2026-09-19").prices_eur_mwh) == 24


def test_an_unreadable_creation_time_leaves_published_absent() -> None:
    body = fixture("synthetic_a44_60m.xml").replace(
        "<createdDateTime>2026-09-18T12:31:00Z</createdDateTime>", "<createdDateTime>not a time</createdDateTime>", 1
    )
    assert parse(body, "DE-LU", "2026-09-19").published is None


def _refusal_cases() -> list[tuple[str, str, str]]:
    base = fixture("synthetic_a44_60m.xml")
    eic = DE_LU_EIC

    def edit(old: str, new: str) -> str:
        assert old in base, f"the fixture has no {old!r}, so this case would prove nothing"
        return base.replace(old, new, 1)

    # The day's last hour removed: every period is still consistent; the hole shows only against the day.
    last = base.rfind("      <Point>")
    closing = base.find("</Point>\n", last) + len("</Point>\n")
    gap = base[:last] + base[closing:]
    return [
        ("a different document type", edit("<type>A44</type>", "<type>A69</type>"), "not A44"),
        (
            "another zone's series and none of ours",
            base.replace(eic, "10YPL-AREA-----S", 2),
            f"no series for {eic}",
        ),
        (
            "a currency that is not EUR",
            edit("<currency_Unit.name>EUR</currency_Unit.name>", "<currency_Unit.name>PLN</currency_Unit.name>"),
            'not "EUR"',
        ),
        (
            "a measure unit that is not MWh",
            edit(
                "<price_Measure_Unit.name>MWh</price_Measure_Unit.name>",
                "<price_Measure_Unit.name>KWH</price_Measure_Unit.name>",
            ),
            "not MWh",
        ),
        (
            "a half-hourly resolution",
            edit("      <resolution>PT60M</resolution>", "      <resolution>PT30M</resolution>"),
            "PT30M",
        ),
        (
            "a resolution this project does not read",
            edit("      <resolution>PT60M</resolution>", "      <resolution>P1D</resolution>"),
            '"P1D"',
        ),
        (
            "an A01 period that starts before the day",
            edit("        <start>2026-09-18T22:00Z</start>", "        <start>2026-09-18T21:00Z</start>"),
            "A01",
        ),
        (
            "an A01 period whose declared end cannot hold its points",
            edit("        <end>2026-09-19T10:00Z</end>", "        <end>2026-09-19T09:00Z</end>"),
            "beyond its own",
        ),
        (
            "an A01 second period overlapping the first",
            edit("        <start>2026-09-19T10:00Z</start>", "        <start>2026-09-19T09:00Z</start>"),
            "A01",
        ),
        (
            "a period that is not a whole number of slots",
            edit("        <start>2026-09-19T10:00Z</start>", "        <start>2026-09-19T10:30Z</start>"),
            "not a whole number of",
        ),
        ("an A01 series missing its last slot", gap, "A01"),
        (
            "an A01 slot past the end of its period",
            edit("        <position>12</position>", "        <position>13</position>"),
            "beyond its own",
        ),
        (
            "an A01 series with a slot missing",
            edit(
                "      <Point>\n        <position>5</position>\n        <price.amount>88.9</price.amount>\n      </Point>\n",
                "",
            ),
            "A01",
        ),
        ("no curveType at all", edit("    <curveType>A01</curveType>\n", ""), "does not interpret"),
        (
            "an unknown curveType",
            edit("<curveType>A01</curveType>", "<curveType>A99</curveType>"),
            "does not interpret",
        ),
        (
            "a duplicated position",
            edit("        <position>2</position>", "        <position>1</position>"),
            "repeats position 1",
        ),
        (
            "a price that is not a finite number",
            edit("        <price.amount>98.42</price.amount>", "        <price.amount>NaN</price.amount>"),
            "not a finite number",
        ),
        (
            "a timestamp that is not UTC",
            edit("        <start>2026-09-18T22:00Z</start>", "        <start>2026-09-19T00:00+02:00</start>"),
            "not a UTC timestamp",
        ),
        ("an empty body", "", "not an A44 document"),
        ("an error page", "<html><body>502 Bad Gateway</body></html>", "not an A44 document"),
        (
            "a document with no series",
            '<Publication_MarketDocument xmlns="urn:x"><type>A44</type></Publication_MarketDocument>',
            "no TimeSeries",
        ),
        # Added here: a point with no price at all (the relay would read it as zero).
        (
            "a point with no price",
            edit("        <price.amount>98.42</price.amount>", "        <price.amount></price.amount>"),
            "has no price",
        ),
        ("a price that is not a number", edit("<price.amount>98.42</price.amount>", "<price.amount>9,8</price.amount>"), "not a number"),
        (
            "a document type definition",
            base.replace("<?xml version=\"1.0\" encoding=\"UTF-8\"?>", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]>', 1),
            "not an A44 document",
        ),
    ]


@pytest.mark.parametrize(("name", "body", "want"), _refusal_cases(), ids=[case[0] for case in _refusal_cases()])
def test_the_parser_refuses_rather_than_repairing(name: str, body: str, want: str) -> None:
    message = refuse(body, "DE-LU", "2026-09-19")
    assert want in message, message


def test_a_real_quarter_hourly_day() -> None:
    parsed = parse(fixture("entsoe_SE4_2026-09-21.xml"), "SE4", "2026-09-21")
    assert len(parsed.prices_eur_mwh) == 96
    assert parsed.resolution_minutes == 15
    assert parsed.start == midnight(area("SE4"), "2026-09-21")
    assert parsed.source == "entsoe.eu" and parsed.published is not None


def test_a_real_day_with_negative_prices() -> None:
    parsed = parse(fixture("entsoe_NO2_2026-09-19_negative.xml"), "NO2", "2026-09-19")
    assert min(parsed.prices_eur_mwh) < 0


def test_a_real_document_carrying_another_day() -> None:
    # The relay's reading: the Helsinki day, an hour of the CET day before and 23 of the CET day itself.
    helsinki = parse(fixture("entsoe_FI_2026-09-21_multiday.xml"), local_area("FI"), "2026-09-21")
    assert len(helsinki.prices_eur_mwh) == 96
    assert helsinki.start == utc("2026-09-20T21:00Z")
    # Home Assistant's: the CET delivery day, which this document carries whole as its second series.
    cet = parse(fixture("entsoe_FI_2026-09-21_multiday.xml"), "FI", "2026-09-21")
    assert len(cet.prices_eur_mwh) == 96
    assert cet.start == utc("2026-09-20T22:00Z")
    assert cet.prices_eur_mwh[:92] == helsinki.prices_eur_mwh[4:]
    # And the CET day before it, also whole in the same document.
    before = parse(fixture("entsoe_FI_2026-09-21_multiday.xml"), "FI", "2026-09-20")
    assert before.start == utc("2026-09-19T22:00Z") and before.prices_eur_mwh[-4:] == helsinki.prices_eur_mwh[:4]


@pytest.mark.parametrize(
    ("name", "area_id", "day"),
    [("entsoe_NO1_2026-09-21_duplicate.xml", "NO1", "2026-09-21"), ("entsoe_PL_2026-09-21.xml", "PL", "2026-09-21")],
)
def test_a_real_document_that_publishes_the_same_curve_twice(name: str, area_id: str, day: str) -> None:
    assert len(parse(fixture(name), area_id, day).prices_eur_mwh) == 96


def test_a_real_sparse_a03_day_carries_values_forward() -> None:
    parsed = parse(fixture("entsoe_SE3_2026-09-21_a03_blocks.xml"), "SE3", "2026-09-21")
    assert len(parsed.prices_eur_mwh) == 96
    # The capture states slots 28 and 30 and not 29. Slot 29 is slot 28's price.
    assert parsed.prices_eur_mwh[28] == parsed.prices_eur_mwh[27]
    assert parsed.prices_eur_mwh[29] != parsed.prices_eur_mwh[28]


def test_the_real_de_lu_document_publishes_the_sequence_its_zone_names() -> None:
    body = fixture("entsoe_DE-LU_2026-09-21_sequences.xml")
    parsed = parse(body, "DE-LU", "2026-09-21")
    assert len(parsed.prices_eur_mwh) == 96
    # 50.68 EUR/MWh is sequence 1's first slot; 112.10 is sequence 2's.
    assert parsed.prices_eur_mwh[0] == 50.68
    assert 112.1 not in parsed.prices_eur_mwh

    without_policy = EntsoeArea(area_id="DE-LU", eic=DE_LU_EIC, market_tz="Europe/Berlin")
    message = refuse(body, without_policy, "2026-09-21")
    for want in ("sequences 1 and 2", "disagree", "50.68 EUR/MWh", "112.10"):
        assert want in message, message
    no_leak(message, "classificationSequence")


def series_block(body: str, label: str) -> str:
    for block in re.findall(r"\n *<TimeSeries>.*?</TimeSeries>", body, flags=re.S):
        if f"<classificationSequence_AttributeInstanceComponent.position>{label}<" in block:
            return block
    raise AssertionError(f"the document carries no series labelled {label}")


def test_a_third_unknown_de_lu_sequence_refuses_the_day() -> None:
    body = fixture("entsoe_DE-LU_2026-09-21_sequences.xml")
    third = series_block(body, "2").replace("<mRID>2</mRID>", "<mRID>3</mRID>", 1)
    third = third.replace(
        "<classificationSequence_AttributeInstanceComponent.position>2<",
        "<classificationSequence_AttributeInstanceComponent.position>3<",
        1,
    )
    with_third = body.replace("</Publication_MarketDocument>", third + "</Publication_MarketDocument>", 1)
    assert ">3<" in with_third
    message = refuse(with_third, "DE-LU", "2026-09-21")
    assert "sequence 3" in message
    no_leak(message)

    # Sequence 1 alone still publishes, so the refusal above is about the label.
    two_series = body.replace(series_block(body, "2"), "", 1)
    assert parse(two_series, "DE-LU", "2026-09-21").prices_eur_mwh[0] == 50.68

    # The named sequence gone: refused, never answered from the other curve.
    missing = body.replace(series_block(body, "1"), "", 1)
    assert "no sequence 1" in refuse(missing, "DE-LU", "2026-09-21")


@pytest.mark.parametrize(
    ("name", "day", "rows", "lowest"),
    [
        ("entsoe_NL_2026-09-21.xml", "2026-09-21", 96, 0.03698),
        ("entsoe_NL_2026-03-29_dst_spring.xml", "2026-03-29", 92, -0.00206),
        ("entsoe_NL_2025-10-26_dst_autumn.xml", "2025-10-26", 100, -0.00104),
        ("entsoe_NL_2026-05-24_negative.xml", "2026-05-24", 96, -0.0865),
    ],
)
def test_the_dutch_captures_normalise_into_whole_days(name: str, day: str, rows: int, lowest: float) -> None:
    target = area("NL")
    parsed = parse_day(fixture(name), target, d(day))
    assert len(parsed.prices_eur_mwh) == rows
    assert parsed.resolution_minutes == 15
    amsterdam = ZoneInfo("Europe/Amsterdam")
    assert parsed.start.astimezone(amsterdam).hour == 0
    end = (starts(parsed)[-1] + timedelta(minutes=15)).astimezone(amsterdam)
    assert end.date() == d(day) + timedelta(days=1) and end.hour == 0
    same(min(kwh(parsed)), lowest, "lowest price")
    assert (lowest < 0) == any(price < 0 for price in parsed.prices_eur_mwh)
    assert parsed.source == SOURCE_NAME


def test_the_dutch_zone_carries_its_verified_identity() -> None:
    nl = area("NL")
    assert nl.eic == "10YNL----------L" and len(nl.eic) == 16
    assert nl.market_tz == "Europe/Amsterdam"
    assert nl.sequence is None and not nl.half_hourly


@pytest.mark.parametrize(
    ("name", "area_id", "day", "hours"),
    [
        ("entsoe_SE4_2026-03-29_dst_spring.xml", "SE4", "2026-03-29", 23),
        ("entsoe_SE4_2025-10-26_dst_autumn.xml", "SE4", "2025-10-26", 25),
    ],
)
def test_real_clock_change_days_have_the_right_boundaries(name: str, area_id: str, day: str, hours: int) -> None:
    body = fixture(name)
    period = body[body.index("<Period>") :]
    start = utc(period[period.index("<start>") + 7 : period.index("</start>")].strip())
    end = utc(period[period.index("<end>") + 5 : period.index("</end>")].strip())
    assert end - start == timedelta(hours=hours)
    parsed = parse(body, area_id, day)
    assert len(parsed.prices_eur_mwh) == hours * 4
    assert parsed.start == midnight(area(area_id), day)


def test_a_real_no_data_acknowledgement() -> None:
    with pytest.raises(EntsoeNoData):
        parse(fixture("entsoe_SE4_2026-09-24_no_data.xml"), "SE4", "2026-09-24")


def test_a_pre_publication_prefix_is_missing_rather_than_a_failure() -> None:
    # A real A44 for the zone with only the first hour of the day: the platform still publishing.
    prefix = entsoe_body(DE_LU_EIC, "2026-09-18T22:00Z", "2026-09-18T23:00Z", "PT60M", ["42.00"])
    with pytest.raises(EntsoeNoData):
        parse(with_sequence(prefix, "1"), "DE-LU", "2026-09-19")
    # The same without the sequence label, for a zone that names none.
    nl_prefix = entsoe_body(NL_EIC, "2026-09-18T22:00Z", "2026-09-18T23:00Z", "PT60M", ["42.00"])
    with pytest.raises(EntsoeNoData):
        parse(nl_prefix, "NL", "2026-09-19")


def test_a_malformed_partial_day_remains_a_failure() -> None:
    # A two-hour A01 period with one stated slot is not a leading prefix: it contradicts itself.
    broken = entsoe_body(DE_LU_EIC, "2026-09-18T22:00Z", "2026-09-19T00:00Z", "PT60M", ["42.00"])
    refuse(broken, "DE-LU", "2026-09-19")


# --- entsoe_a03_test.go --------------------------------------------------------------------------------------

NL_START = "2026-09-20T22:00Z"
NL_END = "2026-09-21T22:00Z"


def parse_a03(body: str):
    return parse(body, "NL", "2026-09-21")


def refuse_a03(body: str) -> str:
    return refuse(body, "NL", "2026-09-21")


def test_an_unstated_slot_carries_the_previous_value() -> None:
    parsed = parse_a03(a03_document(NL_EIC, NL_START, NL_END, "PT60M", (1, "10"), (3, "20"), (5, "30")))
    assert len(parsed.prices_eur_mwh) == 24
    for slot, want in ((1, 0.01), (2, 0.01), (3, 0.02), (4, 0.02), (5, 0.03), (24, 0.03)):
        same(kwh(parsed)[slot - 1], want, f"slot {slot}")


def test_several_consecutive_slots_share_one_value() -> None:
    parsed = parse_a03(a03_document(NL_EIC, NL_START, NL_END, "PT15M", (1, "40"), (5, "44.5"), (9, "41.25")))
    prices = kwh(parsed)
    assert len(prices) == 96
    for slot in range(1, 5):
        same(prices[slot - 1], 0.04)
    for slot in range(5, 9):
        same(prices[slot - 1], 0.0445)
    for slot in range(9, 97):
        same(prices[slot - 1], 0.04125)


def test_the_last_block_runs_to_the_end_of_the_period() -> None:
    parsed = parse_a03(a03_document(NL_EIC, NL_START, NL_END, "PT60M", (1, "10"), (22, "99")))
    same(kwh(parsed)[23], 0.099, "the last slot")
    same(kwh(parsed)[21], 0.099, "slot 22")


def test_a_period_starting_before_the_day_carries_into_it() -> None:
    parsed = parse_a03(a03_document(NL_EIC, "2026-09-20T21:00Z", NL_END, "PT60M", (1, "77"), (14, "88")))
    assert len(parsed.prices_eur_mwh) == 24
    same(kwh(parsed)[0], 0.077, "the day's first slot")
    same(kwh(parsed)[12], 0.088, "slot 13 of the day")


def test_a03_without_a_first_block_is_refused() -> None:
    message = refuse_a03(a03_document(NL_EIC, NL_START, NL_END, "PT60M", (2, "10"), (5, "20")))
    assert "first block is at slot 2" in message


def test_a03_with_a_duplicate_slot_is_refused() -> None:
    message = refuse_a03(a03_document(NL_EIC, NL_START, NL_END, "PT60M", (1, "10"), (4, "20"), (4, "30")))
    assert "repeats position 4" in message


def test_a03_out_of_order_slots_are_sorted() -> None:
    parsed = parse_a03(a03_document(NL_EIC, NL_START, NL_END, "PT60M", (5, "30"), (1, "10"), (3, "20")))
    same(kwh(parsed)[0], 0.01)
    same(kwh(parsed)[2], 0.02)
    same(kwh(parsed)[4], 0.03)


def test_a03_with_a_slot_beyond_the_period_is_refused() -> None:
    message = refuse_a03(a03_document(NL_EIC, NL_START, NL_END, "PT60M", (1, "10"), (25, "30")))
    assert "beyond its own 24 slots" in message


def test_a03_with_an_impossible_slot_or_value_is_refused() -> None:
    assert "not a slot number" in refuse_a03(a03_document(NL_EIC, NL_START, NL_END, "PT60M", (0, "10")))
    assert "not a finite number" in refuse_a03(a03_document(NL_EIC, NL_START, NL_END, "PT60M", (1, "NaN")))


def test_the_same_sparse_body_is_refused_as_a01() -> None:
    sparse = a03_document(NL_EIC, NL_START, NL_END, "PT60M", (1, "10"), (5, "20"))
    message = refuse_a03(sparse.replace("<curveType>A03</curveType>", "<curveType>A01</curveType>", 1))
    assert "A01" in message and "2 of its 24" in message


def test_a_dense_a01_document_is_accepted() -> None:
    blocks = [(slot, str(slot)) for slot in range(1, 25)]
    body = a03_document(NL_EIC, NL_START, NL_END, "PT60M", *blocks).replace(
        "<curveType>A03</curveType>", "<curveType>A01</curveType>", 1
    )
    parsed = parse_a03(body)
    assert len(parsed.prices_eur_mwh) == 24
    same(kwh(parsed)[0], 0.001)
    same(kwh(parsed)[23], 0.024)


# --- entsoe_mixed_test.go ------------------------------------------------------------------------------------

MIXED = "synthetic_a44_FI_2025-10-01_mixed_resolution.xml"


def test_eet_day_across_the_quarter_hour_switch_is_published_at_fifteen_minutes() -> None:
    got = parse(fixture(MIXED), local_area("FI"), "2025-10-01")
    assert len(got.prices_eur_mwh) == 96
    assert got.resolution_minutes == 15
    assert got.start == utc("2025-09-30T21:00Z")
    prices = kwh(got)
    # The first hour is the hourly price, repeated for each of its quarters.
    for i in range(4):
        assert abs(prices[i] - 0.0614) < 1e-9
    assert abs(prices[4] - 0.05) < 1e-9 and abs(prices[5] - 0.0526) < 1e-9 and abs(prices[95] - 0.0591) < 1e-9
    # What a day document gets: an ordinary 15-minute day, nothing new in it.
    fields = day_document_fields(got)
    assert fields["res"] == 15 and len(fields["prices_eur_kwh"]) == 96


def test_the_cet_day_of_an_eet_area_never_meets_the_mix() -> None:
    # Asked by the CET delivery day, Finland's 2025-10-01 lies wholly after the switch and 2025-09-30 wholly
    # before it: the case the relay's local day had to solve does not arise for Home Assistant.
    after = parse(fixture(MIXED), "FI", "2025-10-01")
    assert len(after.prices_eur_mwh) == 96 and after.resolution_minutes == 15
    assert after.start == utc("2025-09-30T22:00Z")
    before = parse(fixture(MIXED), "FI", "2025-09-30")
    assert len(before.prices_eur_mwh) == 24 and before.resolution_minutes == 60


def test_days_on_one_side_of_the_switch_keep_their_own_resolution() -> None:
    cet = fixture(MIXED).replace(FI_EIC, "10Y1001A1001A46L")
    got = parse(cet, "SE3", "2025-10-01")
    assert len(got.prices_eur_mwh) == 96 and got.resolution_minutes == 15
    got = parse(cet, "SE3", "2025-09-30")
    assert len(got.prices_eur_mwh) == 24 and got.resolution_minutes == 60


MIXED_CASES = [
    (
        "one hour at PT60M then 23 hours at PT15M",
        [("2025-09-30T21:00Z", "2025-09-30T22:00Z", "PT60M", 1), ("2025-09-30T22:00Z", "2025-10-01T21:00Z", "PT15M", 92)],
        "",
    ),
    (
        "a quarter-hour missing after the hourly price",
        [("2025-09-30T21:00Z", "2025-09-30T22:00Z", "PT60M", 1), ("2025-09-30T22:15Z", "2025-10-01T21:00Z", "PT15M", 91)],
        "the day has a gap, a repeat or an overlapping period",
    ),
    (
        "an hour missing at the start",
        [("2025-09-30T22:00Z", "2025-09-30T23:00Z", "PT60M", 1), ("2025-09-30T23:00Z", "2025-10-01T21:00Z", "PT15M", 88)],
        "not at the requested",
    ),
    (
        "the hourly period overlapping the quarter-hours",
        [("2025-09-30T21:00Z", "2025-09-30T23:00Z", "PT60M", 2), ("2025-09-30T22:00Z", "2025-10-01T21:00Z", "PT15M", 92)],
        "overlapping period",
    ),
    (
        "an hourly period that does not start on the hour",
        [("2025-09-30T20:30Z", "2025-09-30T21:30Z", "PT60M", 1), ("2025-09-30T21:30Z", "2025-10-01T21:00Z", "PT15M", 94)],
        "cannot be split",
    ),
    (
        "the day's last hour missing",
        [("2025-09-30T21:00Z", "2025-09-30T22:00Z", "PT60M", 1), ("2025-09-30T22:00Z", "2025-10-01T20:00Z", "PT15M", 88)],
        "no document for this day",
    ),
]


@pytest.mark.parametrize(("name", "periods", "want"), MIXED_CASES, ids=[case[0] for case in MIXED_CASES])
def test_mixed_resolution_day_is_exact_or_refused(name: str, periods: list, want: str) -> None:
    body = mixed_doc(FI_EIC, *periods)
    if not want:
        got = parse(body, local_area("FI"), "2025-10-01")
        assert len(got.prices_eur_mwh) == 96
        assert got.prices_eur_mwh[0] == got.prices_eur_mwh[3]
        assert starts(got)[4] - got.start == timedelta(hours=1)
        return
    with pytest.raises((EntsoeParseError, EntsoeNoData)) as caught:
        parse(body, local_area("FI"), "2025-10-01")
    assert want in str(caught.value)


def test_mixed_resolution_on_an_autumn_day_is_one_hundred_quarters() -> None:
    body = mixed_doc(
        FI_EIC,
        ("2025-10-25T21:00Z", "2025-10-25T22:00Z", "PT60M", 1),
        ("2025-10-25T22:00Z", "2025-10-26T22:00Z", "PT15M", 96),
    )
    assert len(parse(body, local_area("FI"), "2025-10-26").prices_eur_mwh) == 100


# --- entsoe_sequence_test.go ---------------------------------------------------------------------------------

SEQUENCE_CASES = [
    ("one complete sequence", [hourly("", "10", 24)], ""),
    ("one labelled complete sequence", [hourly("1", "10", 24)], ""),
    ("none and 2, both complete and identical", [hourly("", "10", 24), hourly("2", "10", 24)], ""),
    ("none and 2, both complete and different", [hourly("", "10", 24), hourly("2", "40", 24)], "sequences none and 2"),
    (
        "complete none beside an incomplete 2",
        [hourly("", "10", 24), partial("2", "40", 24, 12)],
        "cannot show which sequence is the product",
    ),
    (
        "incomplete none beside a complete 2",
        [partial("", "10", 24, 12), hourly("2", "40", 24)],
        "cannot show which sequence is the product",
    ),
    ("two incomplete sequences", [partial("", "10", 24, 12), partial("2", "40", 24, 12)], "no price sequence for"),
    (
        "three complete sequences, all the same",
        [hourly("", "10", 24), hourly("1", "10", 24), hourly("2", "10", 24)],
        "",
    ),
    (
        "three complete sequences, one different",
        [hourly("", "10", 24), hourly("1", "10", 24), hourly("2", "11", 24)],
        "cannot choose between them",
    ),
]


@pytest.mark.parametrize(("name", "series", "want"), SEQUENCE_CASES, ids=[case[0] for case in SEQUENCE_CASES])
def test_sequence_combinations_without_a_policy(name: str, series: list[Series], want: str) -> None:
    body = entsoe_doc(NL_EIC, DAY_START, DAY_END, "PT60M", "A03", *series)
    if not want:
        parsed = parse(body, "NL", "2026-09-21")
        assert len(parsed.prices_eur_mwh) == 24
        same(kwh(parsed)[0], 0.01, "slot 1")
        return
    message = refuse(body, "NL", "2026-09-21")
    assert want in message
    no_leak(message)


def test_an_incomplete_sequence_blocks_identity_collapse() -> None:
    body = entsoe_doc(
        NL_EIC, DAY_START, DAY_END, "PT60M", "A03", hourly("", "10", 24), hourly("1", "10", 24), partial("2", "10", 24, 12)
    )
    message = refuse(body, "NL", "2026-09-21")
    assert "2" in message
    no_leak(message)
    mirrored = entsoe_doc(
        NL_EIC, DAY_START, DAY_END, "PT60M", "A03", partial("2", "10", 24, 12), hourly("", "10", 24), hourly("1", "10", 24)
    )
    refuse(mirrored, "NL", "2026-09-21")


@pytest.mark.parametrize(
    ("name", "area_id", "day"),
    [
        ("entsoe_DK1_2026-09-21_sequences.xml", "DK1", "2026-09-21"),
        ("entsoe_DK2_2026-09-19_sequences.xml", "DK2", "2026-09-19"),
        ("entsoe_DK2_2026-09-20_sequences.xml", "DK2", "2026-09-20"),
    ],
)
def test_real_danish_documents_with_two_identical_sequences(name: str, area_id: str, day: str) -> None:
    parsed = parse(fixture(name), area_id, day)
    assert len(parsed.prices_eur_mwh) == 96
    assert parsed.start == midnight(area(area_id), day)


def test_an_area_policy_selects_one_sequence_only() -> None:
    policy = area("DE-LU")
    assert policy.sequence == "1"
    body = entsoe_doc(DE_LU_EIC, DAY_START, DAY_END, "PT60M", "A03", hourly("1", "10", 24), hourly("2", "40", 24))
    same(kwh(parse(body, policy, "2026-09-21"))[0], 0.01, "the policy's sequence")

    body = entsoe_doc(DE_LU_EIC, DAY_START, DAY_END, "PT60M", "A03", hourly("2", "40", 24))
    assert "no sequence 1" in refuse(body, policy, "2026-09-21")

    body = entsoe_doc(DE_LU_EIC, DAY_START, DAY_END, "PT60M", "A03", partial("1", "10", 24, 12), hourly("2", "40", 24))
    assert "does not cover" in refuse(body, policy, "2026-09-21")

    plain = EntsoeArea(area_id="DE-LU", eic=DE_LU_EIC, market_tz="Europe/Berlin")
    refuse(body, plain, "2026-09-21")


def test_a_period_that_is_not_a_whole_number_of_slots_is_refused() -> None:
    body = entsoe_doc(NL_EIC, "2026-09-20T22:00Z", "2026-09-20T23:05Z", "PT15M", "A03", hourly("", "10", 5))
    assert "not a whole number of" in refuse(body, "NL", "2026-09-21")
    # A period that ends before it starts is never a day. It ends at the day's start, so it does not overlap
    # the day and is skipped as another day's period; with nothing else, the relay reads that as nothing
    # published yet, and so does this port.
    backwards = entsoe_doc(NL_EIC, "2026-09-21T00:00Z", "2026-09-20T22:00Z", "PT60M", "A03", hourly("", "10", 24))
    with pytest.raises((EntsoeParseError, EntsoeNoData)):
        parse(backwards, "NL", "2026-09-21")
    # Inside the day it is refused for what it is.
    inside = entsoe_doc(NL_EIC, "2026-09-21T02:00Z", "2026-09-21T01:00Z", "PT60M", "A03", hourly("", "10", 24))
    assert "ends at or before it starts" in refuse(inside, "NL", "2026-09-21")


DE_LU_CASES = [
    ("sequence 1 complete and alone", [hourly("1", "10", 24)], ""),
    ("sequence 1 beside the known sequence 2", [hourly("1", "10", 24), hourly("2", "40", 24)], ""),
    ("sequence 1 missing", [hourly("2", "40", 24)], "no sequence 1"),
    ("sequence 1 incomplete", [partial("1", "10", 24, 12), hourly("2", "40", 24)], "does not cover"),
    (
        "a previously unknown sequence 3",
        [hourly("1", "10", 24), hourly("2", "40", 24), hourly("3", "70", 24)],
        "sequence 3",
    ),
]


@pytest.mark.parametrize(("name", "series", "want"), DE_LU_CASES, ids=[case[0] for case in DE_LU_CASES])
def test_the_de_lu_sequence_rules_case_by_case(name: str, series: list[Series], want: str) -> None:
    body = entsoe_doc(DE_LU_EIC, DAY_START, DAY_END, "PT60M", "A03", *series)
    if not want:
        parsed = parse(body, "DE-LU", "2026-09-21")
        same(kwh(parsed)[0], 0.01, "the published sequence")
        assert len(parsed.prices_eur_mwh) == 24
        return
    message = refuse(body, "DE-LU", "2026-09-21")
    assert want in message
    no_leak(message)


def test_an_unlabelled_series_refuses_the_zone_that_expects_labels() -> None:
    body = entsoe_doc(DE_LU_EIC, DAY_START, DAY_END, "PT60M", "A03", hourly("1", "10", 24), hourly("", "40", 24))
    assert "sequence none" in refuse(body, "DE-LU", "2026-09-21")


def test_a_zone_with_a_policy_and_no_expected_list_expects_only_its_own() -> None:
    strict = EntsoeArea(area_id="DE-LU", eic=DE_LU_EIC, market_tz="Europe/Berlin", sequence="1")
    alone = entsoe_doc(DE_LU_EIC, DAY_START, DAY_END, "PT60M", "A03", hourly("1", "10", 24))
    parse(alone, strict, "2026-09-21")
    with_second = entsoe_doc(DE_LU_EIC, DAY_START, DAY_END, "PT60M", "A03", hourly("1", "10", 24), hourly("2", "40", 24))
    refuse(with_second, strict, "2026-09-21")
    # The policy itself is always expected, whatever the list says.
    forgot = EntsoeArea(
        area_id="DE-LU", eic=DE_LU_EIC, market_tz="Europe/Berlin", sequence="1", expected_sequences=("2",)
    )
    parse(with_second, forgot, "2026-09-21")


# --- entsoe_at_ie_test.go ------------------------------------------------------------------------------------


def test_austria_publishes_sequence_one_beside_exaa() -> None:
    at = area("AT")
    first = [185.11, 204.65, 183.92, 174.30]
    seq1 = quarter_series("1", 96, first, 190)
    seq2 = quarter_series("2", 96, [199.99, 210.5], 205)
    seq2.values[10] = seq2.values[50] = ""
    start, end = "2026-10-01T22:00Z", "2026-10-02T22:00Z"
    parsed = parse_day(entsoe_doc(AT_EIC, start, end, "PT15M", "A03", seq2, seq1, seq1), at, d("2026-10-02"))
    assert len(parsed.prices_eur_mwh) == 96
    for i, want in enumerate(first):
        assert abs(parsed.prices_eur_mwh[i] - want) <= 1e-9
    assert parsed.start == utc("2026-10-01T22:00Z")

    refuse(entsoe_doc(AT_EIC, start, end, "PT15M", "A03", seq2), at, "2026-10-02")
    message = refuse(
        entsoe_doc(AT_EIC, start, end, "PT15M", "A03", seq1, seq2, quarter_series("3", 96, [], 50)), at, "2026-10-02"
    )
    assert "sequence 3" in message
    other = quarter_series("1", 96, [185.11, 999], 190)
    refuse(entsoe_doc(AT_EIC, start, end, "PT15M", "A03", seq1, other), at, "2026-10-02")


def test_ireland_reads_thirty_minute_documents_and_no_other_zone_does() -> None:
    body = entsoe_doc(IE_EIC, "2026-10-04T22:00Z", "2026-10-05T22:00Z", "PT30M", "A01", half_hours(48, 100))
    parsed = parse(body, IE_SEM, "2026-10-05")
    assert len(parsed.prices_eur_mwh) == 48
    assert parsed.start.astimezone(ZoneInfo("Europe/Dublin")).strftime("%Y-%m-%d %H:%M") == "2026-10-04 23:00"
    assert parsed.resolution_minutes == 30

    nl_body = entsoe_doc(NL_EIC, "2026-10-04T22:00Z", "2026-10-05T22:00Z", "PT30M", "A01", half_hours(48, 100))
    assert "PT30M" in refuse(nl_body, "NL", "2026-10-05")

    hourly_body = entsoe_doc(IE_EIC, "2025-09-14T22:00Z", "2025-09-15T22:00Z", "PT60M", "A01", half_hours(24, 80))
    assert len(parse(hourly_body, IE_SEM, "2025-09-15").prices_eur_mwh) == 24

    prefix = entsoe_doc(IE_EIC, "2026-10-04T22:00Z", "2026-10-05T10:00Z", "PT30M", "A01", half_hours(24, 100))
    with pytest.raises(EntsoeNoData):
        parse(prefix, IE_SEM, "2026-10-05")

    two = entsoe_doc(
        IE_EIC,
        "2026-10-04T22:00Z",
        "2026-10-05T22:00Z",
        "PT30M",
        "A01",
        Series("1", half_hours(48, 100).values),
        Series("2", half_hours(48, 300).values),
    )
    refuse(two, IE_SEM, "2026-10-05")


def test_irelands_two_identical_hourly_series_collapse() -> None:
    series = half_hours(24, 150)
    series.values[0:3] = ["194.715", "182.475", "176.57"]
    body = entsoe_doc(IE_EIC, "2026-09-28T22:00Z", "2026-09-29T22:00Z", "PT60M", "A01", series, series)
    parsed = parse(body, IE_SEM, "2026-09-29")
    assert len(parsed.prices_eur_mwh) == 24 and parsed.resolution_minutes == 60
    for i, want in enumerate((194.715, 182.475, 176.57)):
        assert abs(parsed.prices_eur_mwh[i] - want) <= 1e-9
    other = half_hours(24, 150)
    other.values[0] = "194.715"
    refuse(
        entsoe_doc(IE_EIC, "2026-09-28T22:00Z", "2026-09-29T22:00Z", "PT60M", "A01", series, other), IE_SEM, "2026-09-29"
    )


@pytest.mark.parametrize(
    ("day", "start", "end", "slots"),
    [("2026-03-29", "2026-03-28T23:00Z", "2026-03-29T22:00Z", 46), ("2026-10-25", "2026-10-24T22:00Z", "2026-10-25T23:00Z", 50)],
)
def test_irelands_clock_change_days_are_forty_six_and_fifty_half_hours(day: str, start: str, end: str, slots: int) -> None:
    body = entsoe_doc(IE_EIC, start, end, "PT30M", "A01", half_hours(slots, 90))
    assert len(parse(body, IE_SEM, day).prices_eur_mwh) == slots


def test_no_bundled_area_admits_half_hours() -> None:
    for area_id in bundled_ids():
        assert not area(area_id).half_hourly, area_id


# --- The CET market day and the day document -----------------------------------------------------------------


@pytest.mark.parametrize("area_id", ["FI", "EE", "LV", "LT", "BG", "GR", "RO"])
def test_an_eet_area_is_read_by_the_cet_day(area_id: str) -> None:
    target = area(area_id)
    assert target.market_tz == "Europe/Brussels"
    assert own_zone(area_id) != target.market_tz
    # A document for the area's own local day is not the CET day: its first hour is the CET day before, and
    # what it holds of the CET day stops an hour short, which reads as the day still being published.
    eet = entsoe_body(target.eic, "2026-09-18T21:00Z", "2026-09-19T21:00Z", "PT60M", [f"{i}" for i in range(24)])
    with pytest.raises(EntsoeNoData):
        parse(eet, target, "2026-09-19")
    cet = entsoe_body(target.eic, "2026-09-18T22:00Z", "2026-09-19T22:00Z", "PT60M", [f"{i}" for i in range(24)])
    assert parse(cet, target, "2026-09-19").start == utc("2026-09-18T22:00Z")


def test_the_day_document_fields() -> None:
    parsed = parse(fixture("synthetic_a44_60m.xml"), "DE-LU", "2026-09-19")
    fields = day_document_fields(parsed)
    assert fields["area_id"] == "DE-LU"
    assert fields["market_day"] == d("2026-09-19")
    assert fields["market_tz"] == "Europe/Berlin"
    assert fields["start"].isoformat() == "2026-09-19T00:00:00+02:00"
    assert fields["res"] == 60
    assert fields["src"] == "entsoe.eu"
    assert fields["published"] == utc("2026-09-18T12:31Z")
    same(fields["prices_eur_kwh"][0], 0.09842)
    assert fields["start"].astimezone(timezone.utc) == parsed.start
