"""Shared pieces of the ENTSO-E parser tests: the areas, the fixtures and the document builders.

The builders are the relay's (`spotnav-relay/internal/source/*_test.go`): `entsoe_body` (one A01 series),
`with_sequence`, `a03_document`, `entsoe_doc` (several series), `mixed_doc` (several periods), with the same
shapes so the ported cases read like the originals.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from custom_components.spotnav.pricing.area_catalogue import BUNDLED_PATH, parse_bundled
from custom_components.spotnav.pricing.entsoe_parse import EntsoeArea, EntsoeDay, market_day_bounds

FIXTURES = Path(__file__).parent / "fixtures" / "entsoe"

#: A token that is not one: a run with it proves where a token would go.
SECRET = "sentinel-token-4c1f9a0e-never-real-7d2b"

_DOCUMENT = json.loads(BUNDLED_PATH.read_text(encoding="utf-8"))
_BUNDLED = parse_bundled(_DOCUMENT)
_OWN_ZONE = {entry["id"]: entry["tz"] for entry in _DOCUMENT["areas"]}

DE_LU_EIC = "10Y1001A1001A82H"
NL_EIC = "10YNL----------L"
FI_EIC = "10YFI-1--------U"
AT_EIC = "10YAT-APG------L"
IE_EIC = "10Y1001A1001A59C"

#: Ireland's SEM is not in the bundled catalogue (no data on the platform since 2026-09-30); its route as
#: the relay states it: the Paris market day, half-hours admitted.
IE_SEM = EntsoeArea(area_id="IE-SEM", eic=IE_EIC, market_tz="Europe/Paris", half_hourly=True)


def area(area_id: str) -> EntsoeArea:
    """The bundled area as the ENTSO-E provider asks for it: by the CET market day."""
    return EntsoeArea.from_routes(area_id, _BUNDLED.routes[area_id])


def bundled_ids() -> list[str]:
    """Every bundled area the platform is asked for."""
    return [area_id for area_id, routes in _BUNDLED.routes.items() if routes.entsoe is not None]


def local_area(area_id: str) -> EntsoeArea:
    """The same area read by its own local calendar, as the relay reads it."""
    return area(area_id).on_calendar(_OWN_ZONE[area_id])


def own_zone(area_id: str) -> str:
    return _OWN_ZONE[area_id]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def d(text: str) -> date:
    return date.fromisoformat(text)


def utc(text: str) -> datetime:
    """`2026-09-18T22:00Z` as an instant."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def starts(day: EntsoeDay) -> list[datetime]:
    step = timedelta(minutes=day.resolution_minutes)
    return [day.start + step * i for i in range(len(day.prices_eur_mwh))]


def kwh(day: EntsoeDay) -> list[float]:
    """The day's prices in EUR/kWh, the unit the relay's assertions are written in."""
    return [price / 1000 for price in day.prices_eur_mwh]


def same(got: float, want: float, what: str = "") -> None:
    assert abs(got - want) <= 1e-12, f"{what} = {got}, want {want}"


def midnight(area_like: EntsoeArea, day: str) -> datetime:
    return market_day_bounds(d(day), area_like.market_tz)[0]


def entsoe_body(eic: str, start: str, end: str, resolution: str, prices: list[str]) -> str:
    points = "".join(
        f"<Point><position>{i + 1}</position><price.amount>{price}</price.amount></Point>"
        for i, price in enumerate(prices)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <type>A44</type>
  <createdDateTime>2026-09-18T12:31:00Z</createdDateTime>
  <period.timeInterval><start>{start}</start><end>{end}</end></period.timeInterval>
  <TimeSeries>
    <in_Domain.mRID>{eic}</in_Domain.mRID>
    <out_Domain.mRID>{eic}</out_Domain.mRID>
    <currency_Unit.name>EUR</currency_Unit.name>
    <price_Measure_Unit.name>MWh</price_Measure_Unit.name>
    <curveType>A01</curveType>
    <Period>
      <timeInterval><start>{start}</start><end>{end}</end></timeInterval>
      <resolution>{resolution}</resolution>
      {points}
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""


def with_sequence(body: str, label: str) -> str:
    return body.replace(
        "<curveType>A01</curveType>",
        "<curveType>A01</curveType><classificationSequence_AttributeInstanceComponent.position>"
        f"{label}</classificationSequence_AttributeInstanceComponent.position>",
        1,
    )


def a03_document(eic: str, start: str, end: str, resolution: str, *blocks: tuple[int, str]) -> str:
    points = "".join(
        f"<Point><position>{position}</position><price.amount>{amount}</price.amount></Point>"
        for position, amount in blocks
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <type>A44</type>
  <createdDateTime>2026-09-21T12:50:21Z</createdDateTime>
  <TimeSeries>
    <in_Domain.mRID>{eic}</in_Domain.mRID>
    <out_Domain.mRID>{eic}</out_Domain.mRID>
    <currency_Unit.name>EUR</currency_Unit.name>
    <price_Measure_Unit.name>MWH</price_Measure_Unit.name>
    <curveType>A03</curveType>
    <Period>
      <timeInterval><start>{start}</start><end>{end}</end></timeInterval>
      <resolution>{resolution}</resolution>
      {points}
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""


#: The hand-built sequence documents' day: the CET (and Amsterdam, Berlin) day 2026-09-21.
DAY_START = "2026-09-20T22:00Z"
DAY_END = "2026-09-21T22:00Z"


@dataclass
class Series:
    """One TimeSeries: its sequence label ("" = unlabelled) and a value per slot ("" = not stated)."""

    sequence: str
    values: list[str]
    end: str = ""


def entsoe_doc(eic: str, start: str, end: str, resolution: str, curve: str, *series: Series) -> str:
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">',
        "  <type>A44</type>",
    ]
    for item in series:
        out += [
            "  <TimeSeries>",
            f"    <in_Domain.mRID>{eic}</in_Domain.mRID>",
            f"    <out_Domain.mRID>{eic}</out_Domain.mRID>",
            "    <currency_Unit.name>EUR</currency_Unit.name>",
            "    <price_Measure_Unit.name>MWH</price_Measure_Unit.name>",
            f"    <curveType>{curve}</curveType>",
        ]
        if item.sequence:
            out.append(
                "    <classificationSequence_AttributeInstanceComponent.position>"
                f"{item.sequence}</classificationSequence_AttributeInstanceComponent.position>"
            )
        out += [
            "    <Period>",
            f"      <timeInterval><start>{start}</start><end>{item.end or end}</end></timeInterval>",
            f"      <resolution>{resolution}</resolution>",
        ]
        out += [
            f"      <Point><position>{i + 1}</position><price.amount>{value}</price.amount></Point>"
            for i, value in enumerate(item.values)
            if value != ""
        ]
        out += ["    </Period>", "  </TimeSeries>"]
    out.append("</Publication_MarketDocument>")
    return "\n".join(out) + "\n"


def hourly(sequence: str, amount: str, slots: int) -> Series:
    """An A03 series that states one value and never moves: the simplest complete day."""
    return Series(sequence, [amount] + [""] * (slots - 1))


def partial(sequence: str, amount: str, slots: int, covered: int) -> Series:
    """A series whose period covers only the day's first [covered] hours."""
    end = utc(DAY_START) + timedelta(hours=covered)
    return Series(sequence, [amount] + [""] * (slots - 1), end=end.strftime("%Y-%m-%dT%H:%MZ"))


def quarter_series(sequence: str, slots: int, first: list[float], base: float) -> Series:
    values = [f"{(first[i] if i < len(first) else base + i % 7):.2f}" for i in range(slots)]
    return Series(sequence, values)


def half_hours(slots: int, base: float) -> Series:
    return Series("", [f"{base + i:.2f}" for i in range(slots)])


def mixed_doc(eic: str, *periods: tuple[str, str, str, int]) -> str:
    """One A01 series with the given (start, end, resolution, slots) periods."""
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">',
        "  <type>A44</type>",
        "  <TimeSeries>",
        f"    <in_Domain.mRID>{eic}</in_Domain.mRID>",
        f"    <out_Domain.mRID>{eic}</out_Domain.mRID>",
        "    <currency_Unit.name>EUR</currency_Unit.name>",
        "    <price_Measure_Unit.name>MWH</price_Measure_Unit.name>",
        "    <curveType>A01</curveType>",
    ]
    for p, (start, end, resolution, slots) in enumerate(periods):
        out += [
            "    <Period>",
            f"      <timeInterval><start>{start}</start><end>{end}</end></timeInterval>",
            f"      <resolution>{resolution}</resolution>",
        ]
        out += [
            f"      <Point><position>{i}</position><price.amount>{10 * (p + 1) + i % 10}</price.amount></Point>"
            for i in range(1, slots + 1)
        ]
        out.append("    </Period>")
    out += ["  </TimeSeries>", "</Publication_MarketDocument>"]
    return "\n".join(out) + "\n"
