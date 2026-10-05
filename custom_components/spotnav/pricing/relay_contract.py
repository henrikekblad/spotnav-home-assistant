"""The SpotNav Relay contract (v1 and v2): typed models and the parsers that build them.

Home Assistant consumes three published documents (`areas.json`, `index.json` and one dated price
document) and this module is the one place that knows their shape. The area list and the index come
in two versions (`/v2/…` first, `/v1/…` from a relay that predates contract v2); a day file is the
same `"v": 1` document under both, read on its own calendar (`market_tz`, else `tz`). It is pure (no `hass`,
session or clock), so every rule is a plain unit test. Field names follow `spotnav-relay`'s
`docs/format.md`. A field the relay documents as optional is held as `None`, never substituted:
zero and absent are different facts.

Every rejection raises [RelayParseError] carrying a stable [ParseCode] that callers may branch on;
the message is for logs only.

Not here: fetching (`price_repository`), deciding how much of a day is enough, or computing a
consumer price. Prices are stored as published, in EUR per kWh, with the catalogue's currency and
unit labels beside them.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from types import MappingProxyType
from typing import Any, Final, Literal, Mapping
from urllib.parse import urlsplit

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

#: Entries already reported as skipped, so a catalogue re-read every hour logs each damaged entry once.
_SKIPPED_LOGGED: set[str] = set()


def _log_skipped(kind: str, label: str, err: Exception) -> None:
    key = f"{kind}:{label}:{err}"
    if key in _SKIPPED_LOGGED:
        return
    _SKIPPED_LOGGED.add(key)
    _LOGGER.warning("Skipping %s %s in the relay document: %s", kind, label, err)


#: The day file and profile version; another is refused rather than read hopefully.
SUPPORTED_VERSION: Final = 1

#: The area list and index versions this client reads: v2 (`market_tz`, `included`, `source`, optional
#: `eic`) and v1. A document is parsed as the version it was asked for and must say so.
CATALOGUE_VERSIONS: Final = (1, 2)

#: The resolutions this client can plan with, in minutes: whole divisors of an hour on the planner's
#: quarter-hour grid (contract v2 adds 30, Great Britain's half-hours). Anything else is refused, not coerced.
SUPPORTED_RESOLUTIONS: Final = (15, 30, 60)

#: The parts of a bill a v2 price may already contain (`included`). An unknown name skips the area: a
#: client that does not know what the price holds would add it a second time.
INCLUDED_FIELDS: Final = ("vat", "tax", "grid_fee")

#: An area id: upper-case letters, digits and hyphens, at most 32 of them (`[A-Z0-9-]{1,32}`).
AREA_ID_PATTERN: Final = re.compile(r"^[A-Z0-9-]{1,32}$")

#: A v2 area's `publication.time`: a local wall-clock time, exactly `HH:MM`.
PUBLICATION_TIME_PATTERN: Final = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")

#: The only unit a day document may be priced in (the relay is EUR-native); anything else is a
#: contract violation, not a conversion.
RELAY_PRICE_UNIT: Final = "EUR/kWh"

ParseCode = Literal[
    "not_json",
    "not_an_object",
    "unsupported_version",
    "missing_field",
    "invalid_field",
    "invalid_timestamp",
    "invalid_number",
    "invalid_resolution",
    "invalid_coverage",
    "duplicate_interval",
    "interval_order",
    "interval_duration",
    "interval_gap",
    "area_mismatch",
    "date_mismatch",
    "unit_mismatch",
    "unknown_area",
    "duplicate_area",
    "unsorted_days",
    "duplicate_day",
    "duplicate_hour",
]


class RelayParseError(ValueError):
    """A document that does not satisfy the contract.

    [code] is the program-facing fact; `str(error)` names the field and reason but never the whole
    document (a rejected body may be a proxy's error page).
    """

    def __init__(self, code: ParseCode, message: str) -> None:
        super().__init__(message)
        self.code: ParseCode = code


def _fail(code: ParseCode, message: str) -> None:
    raise RelayParseError(code, message)


def loads_document(text: str) -> Any:
    """Parse a response body as JSON, refusing the non-finite constants.

    `json.loads` accepts `NaN` and `Infinity`, which are not JSON. A body that is not JSON at all
    (likely a proxy's HTML page) raises [RelayParseError] with `not_json`.
    """

    def refuse(constant: str) -> Any:
        _fail("invalid_number", f"the document contains {constant}, which is not a JSON number")

    try:
        return json.loads(text, parse_constant=refuse)
    except RelayParseError:
        raise
    except (ValueError, TypeError) as err:
        raise RelayParseError("not_json", f"the response body is not JSON: {type(err).__name__}") from err


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("not_an_object", f"{what} must be a JSON object")
    return value


def _require(document: Mapping[str, Any], key: str, what: str) -> Any:
    if key not in document:
        _fail("missing_field", f"{what} has no {key!r}")
    return document[key]


def _version(document: Mapping[str, Any], what: str, expected: int = SUPPORTED_VERSION) -> int:
    raw = _require(document, "v", what)
    if isinstance(raw, bool) or not isinstance(raw, int):
        _fail("invalid_field", f"{what}: 'v' must be a whole number, not {raw!r}")
    if raw != expected:
        _fail("unsupported_version", f"{what}: contract version {raw} is not {expected}")
    return raw


def document_version(document: Any) -> int | None:
    """The `v` a stored area list or index says it is, when it is one this client reads, else `None`."""
    if not isinstance(document, dict):
        return None
    raw = document.get("v")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw not in CATALOGUE_VERSIONS:
        return None
    return raw


def _text(document: Mapping[str, Any], key: str, what: str) -> str:
    raw = _require(document, key, what)
    if not isinstance(raw, str) or not raw.strip():
        _fail("invalid_field", f"{what}: {key!r} must be a non-empty string, not {raw!r}")
    return raw


def _optional_text(document: Mapping[str, Any], key: str, what: str) -> str | None:
    """`None` when the field is absent or null; `published` and `retrieved` are documented as nullable."""
    if key not in document or document[key] is None:
        return None
    return _text(document, key, what)


def _number(raw: Any, what: str) -> float:
    """A finite JSON number, never a boolean (`True` is an `int` in Python)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        _fail("invalid_number", f"{what} must be a JSON number, not {type(raw).__name__}")
    value = float(raw)
    if not math.isfinite(value):
        _fail("invalid_number", f"{what} must be finite")
    return value


def _optional_number(document: Mapping[str, Any], key: str, what: str) -> float | None:
    """`None` for an absent field, the number for a present one, zero included: an area with VAT `0`
    differs from one that publishes none.
    """
    if key not in document or document[key] is None:
        return None
    return _number(document[key], f"{what}: {key!r}")


def _timestamp(document: Mapping[str, Any], key: str, what: str, *, required: bool = True) -> datetime | None:
    """A timezone-aware instant; a string with no offset is refused rather than assumed local."""
    if key not in document or document[key] is None:
        if required:
            _fail("missing_field", f"{what} has no {key!r}")
        return None
    raw = document[key]
    if not isinstance(raw, str):
        _fail("invalid_timestamp", f"{what}: {key!r} must be a string")
    parsed = dt_util.parse_datetime(raw)
    if parsed is None or parsed.tzinfo is None:
        _fail("invalid_timestamp", f"{what}: {key!r} is not an offset-bearing ISO timestamp")
    return parsed


def _local_date(document: Mapping[str, Any], key: str, what: str) -> date:
    raw = _text(document, key, what)
    try:
        return date.fromisoformat(raw)
    except ValueError as err:
        raise RelayParseError("invalid_field", f"{what}: {key!r} is not an ISO date") from err


def _resolution(raw: Any, what: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        _fail("invalid_resolution", f"{what} must be a whole number of minutes, not {raw!r}")
    if raw not in SUPPORTED_RESOLUTIONS:
        _fail("invalid_resolution", f"{what} is {raw} minutes; the relay publishes {SUPPORTED_RESOLUTIONS}")
    return raw


@dataclass(frozen=True, slots=True)
class AreaPublication:
    """When an area's prices for tomorrow are expected: a wall-clock time in a zone (contract v2, `publication`).

    The day before the market day, at `local_time` in `tz`. A source's own clock, not the area's: ENTSO-E
    is 13:00 Brussels for every bidding zone, Octopus Agile 16:00 London, Spain's PVPC 20:15 Madrid.
    """

    local_time: time
    tz: str


#: An area that states no publication time (every v1 list, and a v2 list before the field) is expected
#: when ENTSO-E's day-ahead prices are: 13:00 Brussels.
DEFAULT_PUBLICATION: Final = AreaPublication(local_time=time(13, 0), tz="Europe/Brussels")


@dataclass(frozen=True, slots=True)
class AreaEntry:
    """One area the relay can price, as its catalogue states it.

    Three separate facts about money, kept separate as the relay keeps them: `currency` is the ISO
    4217 identity, `major_unit` a display label that is not unique (`kr` is SEK, NOK and DKK alike)
    and `minor_unit` the hundredth-unit label. None is derived from another or from the area id or
    timezone. The fiscal fields are the relay's suggestions for a person to override; `None` means
    nothing published and is never rendered as `0.0`.

    Two calendars (contract v2): `tz` is the zone a person reads the area's times in ("today", the
    chart, the plan); `market_tz` is the zone whose calendar day one day file covers. They are one
    zone unless the v2 list says otherwise (Great Britain is shown in London and published on the
    Paris calendar). `included` names what the published price already holds (`vat`, `tax`,
    `grid_fee`): those settings are locked and nothing is added for them. `source` is the v2 list's
    attribution, `None` from a v1 list. `eic` is `None` for an area that has none (a GSP group).
    `publication` is when tomorrow's prices are expected; [DEFAULT_PUBLICATION] unless the v2 list states it.
    """

    id: str
    eic: str | None
    countries: tuple[str, ...]
    name: str
    tz: str
    currency: str
    major_unit: str
    minor_unit: str
    vat_percent: float | None
    suggested_tax: float | None
    suggested_grid_fee: float | None
    market_tz: str = ""
    included: tuple[str, ...] = ()
    source: AreaSource | None = None
    publication: AreaPublication = DEFAULT_PUBLICATION

    def __post_init__(self) -> None:
        if not self.market_tz:
            # One zone for both calendars unless the list names a second one.
            object.__setattr__(self, "market_tz", self.tz)

    @property
    def split_calendar(self) -> bool:
        """Whether a display day is cut from market-day files (`market_tz` differs from `tz`)."""
        return self.market_tz != self.tz

    def includes(self, component: str) -> bool:
        """Whether the published price already contains `vat`, `tax` or `grid_fee`."""
        return component in self.included


@dataclass(frozen=True, slots=True)
class AreaSource:
    """Where an area's prices come from, shown beside them as attribution (contract v2)."""

    name: str
    url: str


@dataclass(frozen=True, slots=True)
class AreaCatalogue:
    """The whole published area list, with the moment the relay generated it."""

    version: int
    generated: datetime
    areas: tuple[AreaEntry, ...]

    def area(self, area_id: str) -> AreaEntry | None:
        """The area, or `None`. An area id is an exact string, never a fuzzy match."""
        for entry in self.areas:
            if entry.id == area_id:
                return entry
        return None

    @property
    def area_ids(self) -> tuple[str, ...]:
        return tuple(entry.id for entry in self.areas)


@dataclass(frozen=True, slots=True)
class IndexArea:
    """One area inside the index: which of its recent days are published.

    `resolution_minutes` is optional; `res_default` covers areas without a per-area value.
    """

    area_id: str
    days: tuple[date, ...]
    resolution_minutes: int | None

    def lists(self, day: date) -> bool:
        return day in self.days


@dataclass(frozen=True, slots=True)
class RelayIndex:
    """The published index: a short recent window per area, and two revisions.

    `areas_rev` is the first twelve hex digits of the `areas.json` hash, so a client polling this
    small document notices a changed catalogue. It is the only revision the contract states.
    """

    version: int
    generated: datetime
    res_default: int | None
    areas_rev: str
    areas: tuple[IndexArea, ...]

    def area(self, area_id: str) -> IndexArea | None:
        for entry in self.areas:
            if entry.area_id == area_id:
                return entry
        return None


@dataclass(frozen=True, slots=True)
class PriceInterval:
    """One priced interval of a day: when it applies, and what it costs.

    `eur_per_kwh` is the market price as published, never adjusted (negative and zero are prices).

    `utc_start`/`utc_end` are the absolute instants; `start`/`end` are the same instants in the
    document's named timezone, for people and local departure times. Build through [from_instants]
    so the local half is derived and the two cannot disagree. A local clock alone is ambiguous
    (02:15 happens twice on the autumn night, never on the spring day), and two aware datetimes
    sharing one `ZoneInfo` compare by wall clock (PEP 495). Every ordering, gap, duration and
    containment question is therefore answered by `utc_start`/`utc_end`; the local halves are for
    reading, display and local-day logic only.
    """

    start: datetime
    end: datetime
    utc_start: datetime
    utc_end: datetime
    eur_per_kwh: float

    @classmethod
    def from_instants(
        cls, utc_start: datetime, utc_end: datetime, *, zone, eur_per_kwh: float
    ) -> PriceInterval:
        """Build one interval from its absolute half, with its local half derived from it."""
        return cls(
            start=utc_start.astimezone(zone),
            end=utc_end.astimezone(zone),
            utc_start=utc_start,
            utc_end=utc_end,
            eur_per_kwh=eur_per_kwh,
        )

    @property
    def duration(self) -> timedelta:
        """Elapsed time, by instant, never a wall-clock subtraction."""
        return self.utc_end - self.utc_start

    def applies_at(self, instant: datetime) -> bool:
        """Whether this interval covers an instant: half-open, `start <= t < end`, so an instant belongs to
        exactly one interval. Compared by instant (on the autumn night the caller's `fold` or offset says
        which 02:15 was meant).
        """
        if instant.tzinfo is None:
            raise ValueError("applies_at needs an aware instant")
        moment = instant.astimezone(dt_util.UTC)
        return self.utc_start <= moment < self.utc_end


@dataclass(frozen=True, slots=True)
class PriceDocument:
    """One dated price document: the day's prices and where they came from.

    `prices` is kept as published (positional) beside the derived `intervals`; both carry the same
    numbers. `published` is the platform's creation time and `retrieved` when the relay fetched it;
    either may be `None`. `fx` is the rate table the relay attached, empty when it had none close in
    time, never another date's rate. `start` is the day's first instant in the document's timezone and
    `start_instant` the same instant in UTC (see [PriceInterval]).

    A parsed day file's `tz` is its own calendar (`market_tz` when the file states one, else `tz`).
    A display day cut from two market-day files (`pricing/market_day.py`) is one document in the
    display zone whose `parts` are the cut files, each with its own rate: a reader that converts
    money iterates [pieces], so an hour from the next file is priced with that file's rate.
    """

    version: int
    area_id: str
    day: date
    tz: str
    start: datetime
    start_instant: datetime
    resolution_minutes: int
    unit: str
    prices: tuple[float, ...]
    intervals: tuple[PriceInterval, ...]
    fx: Mapping[str, float]
    fx_date: date | None
    fx_src: str | None
    src: str | None
    published: datetime | None
    retrieved: datetime | None
    parts: tuple[PriceDocument, ...] = ()

    def pieces(self) -> tuple[PriceDocument, ...]:
        """The documents that carry this day's rates: its `parts` when it was cut from several, else itself."""
        return self.parts or (self,)

    @property
    def interval_count(self) -> int:
        return len(self.intervals)

    @property
    def covers_until(self) -> datetime:
        """Where this document stops pricing, in its own local timezone."""
        return self.intervals[-1].end

    @property
    def covers_until_instant(self) -> datetime:
        """The same stopping point as an absolute instant."""
        return self.intervals[-1].utc_end

    def covers_whole_day(self) -> bool:
        """Whether the last interval reaches the next local midnight, by instant: true for 23-, 24- and
        25-hour days alike, with no count of 96.
        """
        return self.covers_until_instant >= _as_instant(_next_local_midnight(self.start, self.tz))

    def fx_rate(self, currency: str) -> float | None:
        return self.fx.get(currency)


def _as_instant(moment: datetime) -> datetime:
    """The same instant in UTC, the only safe way to compare two of them (aware datetimes sharing one
    `ZoneInfo` compare by wall clock).
    """
    if moment.tzinfo is None:
        _fail("invalid_timestamp", "a timezone-aware instant is required")
    return moment.astimezone(dt_util.UTC)


def _local_midnight(day: date, tz: str) -> datetime:
    """Midnight at the start of a local calendar date, built from the zone's own rules so a skipped or
    doubled midnight is handled.
    """
    return datetime(day.year, day.month, day.day, tzinfo=dt_util.get_time_zone(tz))


def _next_local_midnight(instant: datetime, tz: str) -> datetime:
    """The next local midnight after [instant], which may be 23, 24 or 25 hours away."""
    local = instant.astimezone(dt_util.get_time_zone(tz))
    following = local.date() + timedelta(days=1)
    return _local_midnight(following, tz)


def validate_intervals(intervals: tuple[PriceInterval, ...], what: str) -> None:
    """The geometry every price day must satisfy, whoever built the list.

    Ordering, uniqueness, positive duration and contiguity are checked rather than assumed from the
    wire, and apply to any other source of intervals (an estimate, a restored snapshot). Comparisons
    are by instant, which makes the autumn night legal (local clock goes backwards, instants do not);
    a genuine reversal, duplicate, overlap or gap is refused.
    """
    if len(intervals) < 2:
        _fail("invalid_coverage", f"{what} has {len(intervals)} interval(s); a day is at least two")
    for index, interval in enumerate(intervals):
        if interval.utc_end <= interval.utc_start:
            _fail("interval_duration", f"{what}: interval {index} does not end after it starts")
        if index == 0:
            continue
        previous = intervals[index - 1]
        if interval.utc_start == previous.utc_start:
            _fail("duplicate_interval", f"{what}: interval {index} starts when interval {index - 1} does")
        if interval.utc_start < previous.utc_start:
            _fail("interval_order", f"{what}: interval {index} starts before interval {index - 1}")
        if interval.utc_start != previous.utc_end:
            _fail(
                "interval_gap",
                f"{what}: interval {index} leaves a gap or overlap at {interval.utc_start.isoformat()}",
            )


def _parse_area(raw: Any, version: int = SUPPORTED_VERSION) -> AreaEntry:
    """One catalogue entry as the list of [version] states it; any fault refuses this entry only."""
    area = _object(raw, "an area")
    area_id = _text(area, "id", "an area")
    what = f"area {area_id!r}"
    if not AREA_ID_PATTERN.match(area_id):
        _fail("invalid_field", f"{what}: an id is [A-Z0-9-]{{1,32}}")
    countries = _require(area, "countries", what)
    if not isinstance(countries, list) or not countries or not all(
        isinstance(code, str) and code.strip() for code in countries
    ):
        _fail("invalid_field", f"{what}: 'countries' must be a non-empty list of strings")
    timezone = _text(area, "tz", what)
    if dt_util.get_time_zone(timezone) is None:
        _fail("invalid_field", f"{what}: {timezone!r} is not a known timezone")

    # The v2 properties. A v1 list states none of them, and its zone is both calendars.
    market_tz = timezone
    included: tuple[str, ...] = ()
    source: AreaSource | None = None
    publication = DEFAULT_PUBLICATION
    if version == 1:
        eic: str | None = _text(area, "eic", what)
    else:
        # Optional in v2 (a Great Britain region is a GSP group, not a bidding zone); present means a code.
        eic = _text(area, "eic", what) if "eic" in area else None
        if "market_tz" in area:
            market_tz = _text(area, "market_tz", what)
            if dt_util.get_time_zone(market_tz) is None:
                _fail("invalid_field", f"{what}: {market_tz!r} is not a known timezone")
        included = _included(area, what)
        source = _source(area, what)
        publication = _publication(area, area_id)

    return AreaEntry(
        id=area_id,
        eic=eic,
        countries=tuple(countries),
        name=_text(area, "name", what),
        tz=timezone,
        currency=_text(area, "currency", what),
        major_unit=_text(area, "major_unit", what),
        minor_unit=_text(area, "minor_unit", what),
        vat_percent=_optional_number(area, "vat_percent", what),
        suggested_tax=_optional_number(area, "suggested_tax", what),
        suggested_grid_fee=_optional_number(area, "suggested_grid_fee", what),
        market_tz=market_tz,
        included=included,
        source=source,
        publication=publication,
    )


def _publication(area: Mapping[str, Any], area_id: str) -> AreaPublication:
    """`publication`: absent is [DEFAULT_PUBLICATION]; present is `{"time": "HH:MM", "tz": <zone>}`.

    A malformed one is not a reason to drop the area: its prices are as good as ever, only the time to
    look for them is unknown, so the default is used and the fault logged once. Unknown keys are ignored.
    """
    if "publication" not in area:
        return DEFAULT_PUBLICATION
    raw = area["publication"]
    problem: str | None = None
    if not isinstance(raw, dict):
        problem = "is not an object"
    else:
        clock = raw.get("time")
        zone = raw.get("tz")
        match = PUBLICATION_TIME_PATTERN.fullmatch(clock) if isinstance(clock, str) else None
        if match is None:
            problem = "has no 'time' as HH:MM"
        elif not isinstance(zone, str) or not zone or dt_util.get_time_zone(zone) is None:
            problem = "has no known 'tz'"
        else:
            return AreaPublication(local_time=time(int(match[1]), int(match[2])), tz=zone)
    _log_ignored(area_id, problem, raw)
    return DEFAULT_PUBLICATION


def _log_ignored(area_id: str, problem: str, raw: Any) -> None:
    """Once per area and published value, so a list re-read every hour does not repeat it."""
    key = f"publication:{area_id}:{json.dumps(raw, sort_keys=True, default=str)[:200]}"
    if key in _SKIPPED_LOGGED:
        return
    _SKIPPED_LOGGED.add(key)
    _LOGGER.warning(
        "Area %s in the relay's area list: 'publication' %s; expecting its prices at %s %s",
        area_id,
        problem,
        DEFAULT_PUBLICATION.local_time.strftime("%H:%M"),
        DEFAULT_PUBLICATION.tz,
    )


def _included(area: Mapping[str, Any], what: str) -> tuple[str, ...]:
    """`included`: absent is none; present is a list of known names, each once, in the list's order."""
    if "included" not in area:
        return ()
    raw = area["included"]
    if not isinstance(raw, list):
        _fail("invalid_field", f"{what}: 'included' must be a list")
    names: list[str] = []
    for name in raw:
        if not isinstance(name, str) or name not in INCLUDED_FIELDS:
            _fail("invalid_field", f"{what}: 'included' names something this client does not know")
        if name in names:
            _fail("invalid_field", f"{what}: 'included' names {name!r} twice")
        names.append(name)
    return tuple(names)


def _source(area: Mapping[str, Any], what: str) -> AreaSource:
    """`source` (required in v2): a name to show and an http(s) address to link it to."""
    raw = _object(_require(area, "source", what), f"{what}: 'source'")
    name = _text(raw, "name", f"{what}: 'source'")
    url = _text(raw, "url", f"{what}: 'source'")
    parsed = urlsplit(url.strip())
    # A link a card renders must not be able to run anything.
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        _fail("invalid_field", f"{what}: 'source.url' must be an http or https address")
    return AreaSource(name=name.strip(), url=url.strip())


def parse_catalogue(document: Any, *, version: int = SUPPORTED_VERSION) -> AreaCatalogue:
    """The published area list of the [version] asked for (`/v1/` or `/v2/areas.json`).

    A list of another version is refused: the two are read by different rules. An entry this client cannot
    use (an unknown `included` name, a bad id, zone or source) is skipped and logged once, so it does not
    take the other areas away; a catalogue listing an id twice is refused: one identity, two entries.
    """
    document = _object(document, "the area catalogue")
    if version not in CATALOGUE_VERSIONS:
        _fail("unsupported_version", f"the area catalogue: version {version} is not read here")
    version = _version(document, "the area catalogue", version)
    generated = _timestamp(document, "generated", "the area catalogue")
    raw_areas = _require(document, "areas", "the area catalogue")
    if not isinstance(raw_areas, list):
        _fail("invalid_field", "the area catalogue: 'areas' must be a list")

    entries: list[AreaEntry] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_areas):
        try:
            entry = _parse_area(raw, version)
        except RelayParseError as err:
            # One damaged entry must not hide every other area.
            _log_skipped("area", f"#{position}", err)
            continue
        if entry.id in seen:
            _fail("duplicate_area", f"the area catalogue lists {entry.id!r} twice")
        seen.add(entry.id)
        entries.append(entry)
    return AreaCatalogue(version=version, generated=generated, areas=tuple(entries))


def _parse_index_area(area_id: Any, raw: Any) -> IndexArea:
    what = f"index area {area_id!r}"
    if not isinstance(area_id, str) or not AREA_ID_PATTERN.match(area_id):
        _fail("invalid_field", "the index has an area key that is not an area id")
    area = _object(raw, what)
    raw_days = _require(area, "days", what)
    if not isinstance(raw_days, list):
        _fail("invalid_field", f"{what}: 'days' must be a list")
    days: list[date] = []
    for item in raw_days:
        if not isinstance(item, str):
            _fail("invalid_field", f"{what}: 'days' must hold ISO dates")
        try:
            days.append(date.fromisoformat(item))
        except ValueError as err:
            raise RelayParseError("invalid_field", f"{what}: {item!r} is not an ISO date") from err
    if len(set(days)) != len(days):
        _fail("duplicate_day", f"{what}: 'days' repeats a date")
    if days != sorted(days):
        _fail("unsorted_days", f"{what}: 'days' is not in ascending order")
    resolution = None if area.get("res") is None else _resolution(area["res"], f"{what}: 'res'")
    return IndexArea(area_id=area_id, days=tuple(days), resolution_minutes=resolution)


def parse_index(document: Any, *, version: int = SUPPORTED_VERSION) -> RelayIndex:
    """The published index of the [version] asked for, whose `days` lists are this client's authority.

    The two versions have one shape (v2 lists every advertised area, its days on each area's market
    calendar). An unsorted or repeating list, or a resolution this client cannot plan with, skips that
    area so a damaged one is not read as authoritative.
    """
    document = _object(document, "the index")
    if version not in CATALOGUE_VERSIONS:
        _fail("unsupported_version", f"the index: version {version} is not read here")
    version = _version(document, "the index", version)
    generated = _timestamp(document, "generated", "the index")
    areas_rev = _text(document, "areas_rev", "the index")
    res_default = None if document.get("res_default") is None else _resolution(document["res_default"], "the index: 'res_default'")
    raw_areas = _require(document, "areas", "the index")
    if not isinstance(raw_areas, dict):
        _fail("invalid_field", "the index: 'areas' must be an object keyed by area id")

    entries: list[IndexArea] = []
    for area_id, raw in raw_areas.items():
        try:
            entries.append(_parse_index_area(area_id, raw))
        except RelayParseError as err:
            # One damaged entry must not hide every other area's days.
            _log_skipped("index area", repr(area_id), err)
    return RelayIndex(
        version=version,
        generated=generated,
        res_default=res_default,
        areas_rev=areas_rev,
        areas=tuple(entries),
    )


def parse_day(document: Any, *, area_id: str, day: date) -> PriceDocument:
    """One dated price document, checked against the area and date asked for.

    Identity is checked, not trusted: another area is another market's money, another date another
    day's. A stated unit other than EUR/kWh is refused too.

    Intervals are derived from `start` and `res` by stepping in absolute time, so 23-, 24- and 25-hour
    days come out right, then each endpoint is expressed in the document's timezone with its instant
    kept beside it (a spring day has no 02:15 interval, an autumn day has two). They are validated and
    checked to stay inside the document's local date. A day that stops early is accepted: incomplete
    publication is a state the repository reports, not a parse failure.
    """
    document = _object(document, "the day document")
    version = _version(document, "the day document")
    document_area = _text(document, "area", "the day document")
    if document_area != area_id:
        _fail("area_mismatch", f"the document is for {document_area!r}, not {area_id!r}")
    document_day = _local_date(document, "date", "the day document")
    if document_day != day:
        _fail("date_mismatch", f"the document is for {document_day.isoformat()}, not {day.isoformat()}")

    # A file's calendar is its `market_tz` when it states one (a Great Britain file: shown in London,
    # dated in Paris) and its `tz` otherwise (every v1 zone, Portugal's Madrid included).
    timezone = _text(document, "market_tz" if "market_tz" in document else "tz", "the day document")
    if dt_util.get_time_zone(timezone) is None:
        _fail("invalid_field", f"the day document: {timezone!r} is not a known timezone")
    resolution = _resolution(_require(document, "res", "the day document"), "the day document: 'res'")
    unit = _text(document, "unit", "the day document")
    if unit != RELAY_PRICE_UNIT:
        _fail("unit_mismatch", f"the day document is priced in {unit!r}, not {RELAY_PRICE_UNIT!r}")

    zone = dt_util.get_time_zone(timezone)
    published_start = _timestamp(document, "start", "the day document")
    # Compared by instant: the published `start` carries the relay's offset, the constructed
    # midnight the zone's own.
    start_instant = _as_instant(published_start)
    midnight = _local_midnight(day, timezone)
    if start_instant != _as_instant(midnight):
        # A day document starts at the first instant of its local date: the array is positional,
        # so any other start would misalign every price after it.
        _fail(
            "invalid_coverage",
            f"the day document starts at {published_start.isoformat()}, not at {midnight.isoformat()}",
        )

    raw_prices = _require(document, "prices", "the day document")
    if not isinstance(raw_prices, list):
        _fail("invalid_field", "the day document: 'prices' must be a list")
    prices = tuple(_number(value, f"the day document: prices[{index}]") for index, value in enumerate(raw_prices))

    # Positions are `start + n x res` on the absolute timeline, expressed in the zone afterwards;
    # stepping on the local clock would invent the spring 02:15 and lose the autumn repeat.
    step = timedelta(minutes=resolution)
    intervals = tuple(
        PriceInterval.from_instants(
            start_instant + step * index,
            start_instant + step * (index + 1),
            zone=zone,
            eur_per_kwh=price,
        )
        for index, price in enumerate(prices)
    )
    validate_intervals(intervals, "the day document")
    day_end = _as_instant(_next_local_midnight(published_start, timezone))
    if intervals[-1].utc_end > day_end:
        _fail(
            "invalid_coverage",
            "the day document prices past its own local midnight, ending at "
            f"{intervals[-1].end.isoformat()}",
        )

    return PriceDocument(
        version=version,
        area_id=document_area,
        day=day,
        tz=timezone,
        start=published_start.astimezone(zone),
        start_instant=start_instant,
        resolution_minutes=resolution,
        unit=unit,
        prices=prices,
        intervals=intervals,
        fx=_fx_table(document),
        fx_date=_optional_local_date(document, "fx_date", "the day document"),
        fx_src=_optional_text(document, "fx_src", "the day document"),
        src=_optional_text(document, "src", "the day document"),
        published=_timestamp(document, "published", "the day document", required=False),
        retrieved=_timestamp(document, "retrieved", "the day document", required=False),
    )


@dataclass(frozen=True, slots=True)
class ProfileHour:
    """One local weekday-hour of the history profile: the median and spread of its spot prices.

    EUR per kWh before taxes, VAT or fees, exactly as the relay states them. `n` is how many
    prices the entry aggregates (the relay omits entries below 8).
    """

    median: float
    std: float
    n: int


@dataclass(frozen=True, slots=True)
class PriceProfile:
    """The relay's history profile for one area: median and spread per local weekday x hour.

    Prices are what the market did over `from_date..to_date` (`weeks` full weeks of it), never a
    forecast. `hours` is keyed by `(iso_weekday, local_hour)`; a key that is absent has no usable
    history.
    """

    version: int
    area_id: str
    tz: str
    unit: str
    generated: datetime
    from_date: date
    to_date: date
    weeks: int
    hours: Mapping[tuple[int, int], ProfileHour]

    def hour(self, weekday: int, hour: int) -> ProfileHour | None:
        return self.hours.get((weekday, hour))


def parse_profile(document: Any, *, area_id: str) -> PriceProfile:
    """`/v1/<area>/profile.json`, checked strictly against the area asked for.

    Identity, unit and every number are checked, not trusted: an entry with a weekday outside 1..7, an
    hour outside 0..23, a negative spread or a count below one refuses the whole file, and so does a
    weekday-hour listed twice. Nothing is repaired; a refused profile means "no usable profile".
    """
    what = "the profile document"
    document = _object(document, what)
    version = _version(document, what)
    document_area = _text(document, "area", what)
    if document_area != area_id:
        _fail("area_mismatch", f"the profile is for {document_area!r}, not {area_id!r}")
    timezone = _text(document, "tz", what)
    if dt_util.get_time_zone(timezone) is None:
        _fail("invalid_field", f"{what}: {timezone!r} is not a known timezone")
    unit = _text(document, "unit", what)
    if unit != RELAY_PRICE_UNIT:
        _fail("unit_mismatch", f"{what} is priced in {unit!r}, not {RELAY_PRICE_UNIT!r}")
    generated = _timestamp(document, "generated", what)
    assert generated is not None
    from_date = _local_date(document, "from", what)
    to_date = _local_date(document, "to", what)
    if to_date < from_date:
        _fail("invalid_field", f"{what}: 'to' is before 'from'")
    weeks = _require(document, "weeks", what)
    if isinstance(weeks, bool) or not isinstance(weeks, int) or weeks < 1:
        _fail("invalid_field", f"{what}: 'weeks' must be a positive whole number")
    raw_hours = _require(document, "hours", what)
    if not isinstance(raw_hours, list):
        _fail("invalid_field", f"{what}: 'hours' must be a list")
    hours: dict[tuple[int, int], ProfileHour] = {}
    for index, raw in enumerate(raw_hours):
        entry = _object(raw, f"{what}: hours[{index}]")
        weekday = _require(entry, "weekday", f"hours[{index}]")
        hour = _require(entry, "hour", f"hours[{index}]")
        count = _require(entry, "n", f"hours[{index}]")
        for name, value in (("weekday", weekday), ("hour", hour), ("n", count)):
            if isinstance(value, bool) or not isinstance(value, int):
                _fail("invalid_field", f"{what}: hours[{index}].{name} must be a whole number")
        if not 1 <= weekday <= 7 or not 0 <= hour <= 23 or count < 1:
            _fail("invalid_field", f"{what}: hours[{index}] is out of range")
        median = _number(_require(entry, "median", f"hours[{index}]"), f"{what}: hours[{index}].median")
        std = _number(_require(entry, "std", f"hours[{index}]"), f"{what}: hours[{index}].std")
        if std < 0:
            _fail("invalid_number", f"{what}: hours[{index}].std must not be negative")
        if (weekday, hour) in hours:
            _fail("duplicate_hour", f"{what} lists weekday {weekday} hour {hour} twice")
        hours[(weekday, hour)] = ProfileHour(median=median, std=std, n=count)
    return PriceProfile(
        version=version,
        area_id=document_area,
        tz=timezone,
        unit=unit,
        generated=generated,
        from_date=from_date,
        to_date=to_date,
        weeks=weeks,
        hours=MappingProxyType(hours),
    )


def _optional_local_date(document: Mapping[str, Any], key: str, what: str) -> date | None:
    if key not in document or document[key] is None:
        return None
    return _local_date(document, key, what)


def _fx_table(document: Mapping[str, Any]) -> Mapping[str, float]:
    """The relay's rate table: local = EUR x rate, or empty when it has no rate.

    Every rate must be positive and finite (zero converts nothing, a negative one inverts a price).
    """
    raw = document.get("fx")
    if raw is None:
        return MappingProxyType({})
    table = _object(raw, "the day document: 'fx'")
    rates: dict[str, float] = {}
    for currency, rate in table.items():
        if not isinstance(currency, str) or not currency.strip():
            _fail("invalid_field", "the day document: 'fx' must be keyed by currency code")
        value = _number(rate, f"the day document: fx[{currency!r}]")
        if value <= 0:
            _fail("invalid_number", f"the day document: fx[{currency!r}] is not a rate")
        rates[currency] = value
    return MappingProxyType(rates)
