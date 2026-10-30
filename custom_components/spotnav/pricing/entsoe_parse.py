"""ENTSO-E's Transparency Platform, read without the network: the request, the response and the day document.

A port of the relay's ENTSO-E client (`spotnav-relay/internal/source/entsoe.go` and `token.go`), rule for rule,
minus the transport: no socket, no limiter, no clock of its own. The provider that asks the platform wraps it.

* **The request** ([build_request], [loggable_url]). Day-ahead prices (`documentType=A44`, contract `A01`) for
  one bidding zone (`in_Domain` = `out_Domain` = its EIC), for one **CET market day**: the area's
  `cet_market_tz` midnight to the next one, in UTC (`periodStart`/`periodEnd`, `yyyyMMddHHmm`). A clock-change
  day is therefore 23 or 25 hours without a special case. A zone with a sequence policy (DE-LU, AT) asks for
  its sequence too; the answer is still checked for it.
* **The token** ([EntsoeToken]). Wrapped so that every accidental rendering (`str`, `repr`, `format`, a log
  argument) says `<redacted>`; the only URL handed out for logging is the redacted one. The token-bearing URL
  exists only inside an [EntsoeRequest], which renders as the redacted URL too. A transport error's text can
  hold the whole request URL, so [redact_error] rebuilds it as a new error with the token removed and no
  cause left to reach the original.
* **The response** ([read_response]): HTTP 429 is a rate limit with a bounded `Retry-After`; 401/403 an
  authentication failure whatever body came with them, never "no data"; 5xx a brief back-off; an
  acknowledgement is classified by its reason **text** ([classify_acknowledgement]) because code 999 is
  generic; everything else is parsed.
* **The parser** ([parse_day]). Refuses rather than repairs: a document that is not A44, a currency other
  than EUR, a unit other than MWh, a resolution other than PT15M/PT60M (PT30M only for a half-hourly
  market), an unknown curve type, a period that is not a whole number of slots, an impossible or repeated
  position, a price that is not a finite number, two values for one instant that disagree, sequences it
  cannot choose between, or rows that do not add up to the requested day exactly (a gap, a repeat, an
  overlap). A01 curves state every slot; A03 curves state a value where it changes and carry it forward. A
  day published at two resolutions (an Eastern European local day across SDAC's 15-minute switch on
  2025-10-01) is read at the finer one. An empty answer, or a strict leading part of the day, is the
  platform still publishing: [EntsoeNoData], like the "No matching data" acknowledgement.

Messages carry fixed labels, field names and the platform's numbers, never a response body or a URL.

The parsed day is neutral ([EntsoeDay]: start instant, resolution, EUR/MWh); [day_document_fields] gives the
fields of a `"v":1` day document (EUR/kWh, `src`), which the provider completes with the area's display zone,
the exchange rate and the retrieval time.
"""

from __future__ import annotations

import math
import re
import urllib.parse
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final, Literal, TYPE_CHECKING

from .. import time_zones

if TYPE_CHECKING:
    from .area_catalogue import AreaRoutes

#: The platform's one REST endpoint.
ENDPOINT: Final = "https://web-api.tp.entsoe.eu/api"

#: The published `src` of an ENTSO-E day: the platform's name, deliberately not a URL.
SOURCE_NAME: Final = "entsoe.eu"

#: What every rendering of a token says.
REDACTED: Final = "<redacted>"

#: The most a client should read of one response: a day document is tens of kilobytes.
MAX_BODY_BYTES: Final = 4 << 20

#: Waits after a refusal to serve, for the provider's next attempt (the relay's limiter penalties).
RATE_LIMITED_BACKOFF: Final = timedelta(minutes=2)
SERVER_BACKOFF: Final = timedelta(seconds=30)
MAX_BACKOFF: Final = timedelta(minutes=15)

#: The platform's period format, always UTC.
_PERIOD_FORMAT: Final = "%Y%m%d%H%M"

#: The document's unit is EUR per MWh; a day document's is EUR per kWh.
_PER_KWH: Final = 1000.0
_CURRENCY: Final = "EUR"
_PRICE_UNIT: Final = "mwh"

#: A01 states every slot; A03 states a value where it changes.
_CURVE_STEPWISE: Final = "A01"
_CURVE_VARIABLE: Final = "A03"

#: How far apart two values for one instant may be and still be one price: the relay's 1e-6 EUR/kWh.
#: The platform writes six significant digits, so one number can arrive as 69.34 and 69.339996.
_AGREEMENT_EUR_MWH: Final = 1e-3

_REASON_NO_DATA: Final = "999"
_UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=_UTC)

#: The longest field value a message quotes. Every quoted value is a short code (a type, a currency, a
#: resolution, a time stamp); a longer one is not that field, and is described by its length instead, so not
#: even part of an echoed token reaches a message.
_MAX_QUOTED: Final = 25

#: Stable failure codes of [read_response]. The provider maps them onto the repository's codes.
EntsoeCode = Literal[
    "not_published",  # no document yet: ask again on the publication cadence
    "entsoe_auth",  # the token was rejected (401/403, or an acknowledgement naming a credential problem)
    "rate_limited",  # 429 or a rate-limit acknowledgement; `retry_after` says how long to wait
    "entsoe_unavailable",  # 5xx
    "entsoe_rejected",  # another status, or an acknowledgement that is a bad request or unrecognized
    "provider_refused",  # a body the parser refuses
]


class EntsoeError(Exception):
    """Base of what this module raises."""


class EntsoeNoData(EntsoeError):
    """The platform has no document for the day yet: an outcome, not a failure."""

    def __init__(self) -> None:
        super().__init__("entsoe has no document for this day")


class EntsoeParseError(EntsoeError, ValueError):
    """A body that is not the requested day, with a message naming the field and never quoting the body."""


class EntsoeRequestError(EntsoeError, ValueError):
    """A request that cannot be built: no token, or an area with no address on the platform."""


# --- Areas and the market day --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntsoeArea:
    """One area as the platform is asked for it and its documents are read.

    `market_tz` is the calendar of the day asked for: the area's `cet_market_tz` (the CET delivery day,
    the same file Nord Pool answers). `sequence` names the classification sequence a zone publishes under
    (DE-LU, AT; `None` for one answer), `expected_sequences` every label such a zone's documents may
    carry, `half_hourly` admits PT30M (Ireland's SEM only).
    """

    area_id: str
    eic: str
    market_tz: str
    sequence: str | None = None
    expected_sequences: tuple[str, ...] = ()
    half_hourly: bool = False

    @classmethod
    def from_routes(cls, area_id: str, routes: AreaRoutes) -> EntsoeArea:
        """The area from its bundled routes, asked for by the CET market day."""
        route = routes.entsoe
        if route is None:
            raise EntsoeRequestError(f"area {area_id} has no ENTSO-E route")
        return cls(
            area_id=area_id,
            eic=route.eic,
            market_tz=routes.cet_market_tz,
            sequence=route.sequence,
            expected_sequences=tuple(route.expected_sequences),
            half_hourly=route.half_hourly,
        )

    def on_calendar(self, market_tz: str) -> EntsoeArea:
        """The same area read by another calendar (the relay's local days, for a check against its fixtures)."""
        return replace(self, market_tz=market_tz)


def market_day_bounds(day: date, market_tz: str) -> tuple[datetime, datetime]:
    """The half-open UTC interval of one market day: local midnight to the next local midnight."""
    zone = time_zones.require_zone(market_tz)
    following = day + timedelta(days=1)
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    end = datetime(following.year, following.month, following.day, tzinfo=zone)
    return start.astimezone(_UTC), end.astimezone(_UTC)


# --- The token and the request -------------------------------------------------------------------------------


class EntsoeToken:
    """The security token, which no rendering reveals.

    `str`, `repr`, `format` and `%s`/`%r` all say `<redacted>`, so a token passed to a log call, put in an
    f-string or printed inside a container shows the placeholder. It cannot be pickled or copied into JSON.
    [reveal] is the one way to the value, and [EntsoeRequest.wire_url] its one caller.
    """

    __slots__ = ("_secret",)

    def __init__(self, value: str) -> None:
        # A trailing newline is what an editor leaves behind, not part of a secret.
        self._secret = value.strip()

    @property
    def empty(self) -> bool:
        """No token at all: a configuration fact, safe to test and to log."""
        return not self._secret

    def reveal(self) -> str:
        return self._secret

    def redact(self, text: str) -> str:
        """[text] with every occurrence of the token replaced by the placeholder."""
        if not self._secret:
            return text
        return text.replace(self._secret, REDACTED)

    def __str__(self) -> str:
        return REDACTED

    def __repr__(self) -> str:
        return f"EntsoeToken({REDACTED})"

    def __format__(self, spec: str) -> str:
        return REDACTED

    def __reduce__(self) -> Any:
        raise TypeError("an ENTSO-E token cannot be pickled")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EntsoeToken) and other._secret == self._secret

    def __hash__(self) -> int:
        return hash((EntsoeToken, self._secret))


def query_parameters(area: EntsoeArea, day: date) -> dict[str, str]:
    """The request's parameters without the token: one zone, the day-ahead contract, one market day in UTC."""
    if not area.eic.strip():
        raise EntsoeRequestError(f"area {area.area_id} has no EIC to ask for")
    start, end = market_day_bounds(day, area.market_tz)
    parameters = {
        "documentType": "A44",
        # A01 is the day-ahead contract. Without it the platform also answers with the zone's intraday
        # auctions, labelled as sequences beside (or instead of) the day-ahead curve.
        "contract_MarketAgreement.type": "A01",
        "periodStart": start.strftime(_PERIOD_FORMAT),
        "periodEnd": end.strftime(_PERIOD_FORMAT),
        "in_Domain": area.eic,
        "out_Domain": area.eic,
    }
    # A parameter is a request, not a promise: parse_day checks the answer carries the sequence.
    if sequence := (area.sequence or "").strip():
        parameters["classificationSequence_AttributeInstanceComponent.position"] = sequence
    return parameters


def _url(endpoint: str, parameters: Mapping[str, str]) -> str:
    # Encoded, never concatenated, and sorted: the same URL every time, comparable in a test.
    return f"{endpoint}?{urllib.parse.urlencode(sorted(parameters.items()))}"


def loggable_url(area: EntsoeArea, day: date, *, endpoint: str = ENDPOINT) -> str:
    """The request URL with the token replaced by the placeholder (percent-encoded like any value)."""
    return _url(endpoint, {**query_parameters(area, day), "securityToken": REDACTED})


class EntsoeRequest:
    """One request. It renders as its loggable URL; [wire_url] is for the HTTP client and nothing else."""

    __slots__ = ("_parameters", "_token", "endpoint", "loggable_url")

    def __init__(self, token: EntsoeToken, parameters: Mapping[str, str], endpoint: str) -> None:
        self._token = token
        self._parameters = dict(parameters)
        self.endpoint = endpoint
        self.loggable_url = _url(endpoint, {**self._parameters, "securityToken": REDACTED})

    def wire_url(self) -> str:
        """The URL with the token in it. Hand it to the HTTP client; never to a message, a log or a store."""
        return _url(self.endpoint, {**self._parameters, "securityToken": self._token.reveal()})

    @property
    def token(self) -> EntsoeToken:
        """The token the request carries, so that the caller can redact a transport error's text."""
        return self._token

    def __str__(self) -> str:
        return self.loggable_url

    def __repr__(self) -> str:
        return f"EntsoeRequest({self.loggable_url})"

    def __reduce__(self) -> Any:
        raise TypeError("an ENTSO-E request cannot be pickled")


def build_request(token: EntsoeToken, area: EntsoeArea, day: date, *, endpoint: str = ENDPOINT) -> EntsoeRequest:
    """The request for one area's market day. An empty token is refused: a request that cannot work."""
    if token.empty:
        raise EntsoeRequestError("entsoe: no security token configured")
    return EntsoeRequest(token, query_parameters(area, day), endpoint)


class EntsoeTransportError(EntsoeError):
    """A failed request's message with the token removed, and no way back to the error it came from.

    Whether it timed out or was cancelled is kept as two flags rather than as the original error object,
    whose text and attributes may hold the whole request URL. Raise it **outside** the `except` block that
    caught the original: a raise inside it, even `from None`, links the original as `__context__`.
    """

    def __init__(self, message: str, *, timeout: bool = False, cancelled: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout
        self.cancelled = cancelled


def _chain(err: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = err
    while current is not None and all(current is not other for other in seen):
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def redact_error(token: EntsoeToken, err: BaseException) -> EntsoeTransportError:
    """[err] as a sanitized error: its message with the token removed, its timeout/cancel identity kept."""
    # CancelledError is not an Exception subclass; imported here so the module stays free of asyncio otherwise.
    from asyncio import CancelledError

    chain = _chain(err)
    redacted = EntsoeTransportError(
        token.redact(str(err)),
        timeout=any(isinstance(item, TimeoutError) for item in chain),
        cancelled=any(isinstance(item, CancelledError) for item in chain),
    )
    redacted.__cause__ = None
    redacted.__context__ = None
    redacted.__suppress_context__ = True
    return redacted


# --- XML -----------------------------------------------------------------------------------------------------


def _local(tag: Any) -> str:
    # Element names are matched without their namespace: the schema's namespace has been renamed more
    # than once and none of those renames changed a name or its meaning.
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _root(body: bytes | str) -> ElementTree.Element | None:
    """The parsed document, or `None` for a body that is not well-formed XML (or declares a DTD)."""
    head = body[:4096] if isinstance(body, str) else body[:4096].decode("latin-1")
    # The platform never sends a DTD; refusing one keeps entity expansion out of the parser entirely.
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        return None
    try:
        return ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element if _local(child.tag) == name]


def _text(element: ElementTree.Element, *path: str) -> str:
    """The text of the first element down [path], or "" when there is none (what the relay reads)."""
    current: ElementTree.Element | None = element
    for name in path:
        found = _children(current, name) if current is not None else []
        current = found[0] if found else None
    if current is None:
        return ""
    return current.text or ""


def _quote(value: str) -> str:
    value = value.strip()
    if len(value) > _MAX_QUOTED:
        return f"a value of {len(value)} characters"
    return f'"{value}"'


# --- Acknowledgements ----------------------------------------------------------------------------------------


class AckKind(StrEnum):
    """What an acknowledgement means. The values are the fixed labels messages carry."""

    NO_DATA = "no matching data"
    AUTH = "authentication or permission problem"
    RATE_LIMIT = "rate limited"
    BAD_REQUEST = "request rejected"
    UNKNOWN = "unrecognized acknowledgement"


#: Phrases recognized in a normalized reason text. Few and specific: a list that guessed would classify a
#: real failure as "no data", the one outcome that stops anybody looking.
_NO_DATA_PHRASES: Final = ("no matching data",)
_AUTH_PHRASES: Final = (
    "securitytoken", "security token", "token is not valid", "invalid token",
    "authentication", "authenticated", "not authorized", "unauthorized",
    "permission", "access denied", "not allowed",
)  # fmt: skip
_RATE_LIMIT_PHRASES: Final = ("rate limit", "too many requests", "quota")
_BAD_REQUEST_PHRASES: Final = (
    "malformed", "not a valid", "invalid parameter", "missing parameter",
    "not supported", "unsupported", "out of range", "outside the range",
)  # fmt: skip


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _contains_any(text: str, phrases: Sequence[str]) -> bool:
    return any(phrase in text for phrase in phrases)


@dataclass(frozen=True, slots=True)
class _Acknowledgement:
    reasons: tuple[tuple[str, str], ...]  # (code, text)

    def reason_code(self) -> str:
        """The first reason's code, for a message."""
        for code, _ in self.reasons:
            if code.strip():
                return code.strip()
        return "none"

    def no_data_only(self) -> bool:
        """Every reason with a text says no matching data, and at least one does."""
        said = False
        for _, text in self.reasons:
            normalized = _normalize(text)
            if not normalized:
                continue
            if not _contains_any(normalized, _NO_DATA_PHRASES):
                return False
            said = True
        return said

    def kind(self, status: int) -> AckKind:
        """The meaning, by precedence: a credential complaint anywhere is authentication, even beside "no
        matching data"; then a rate limit; then a rejected request; then no data, only when every reason
        says so and the status is one such an answer arrives with (200, 400); anything else is unknown.
        """
        if self.reason_code() != _REASON_NO_DATA:
            return AckKind.UNKNOWN
        text = _normalize(" ".join(text for _, text in self.reasons))
        if _contains_any(text, _AUTH_PHRASES):
            return AckKind.AUTH
        if _contains_any(text, _RATE_LIMIT_PHRASES):
            return AckKind.RATE_LIMIT
        if _contains_any(text, _BAD_REQUEST_PHRASES):
            return AckKind.BAD_REQUEST
        if self.no_data_only() and status in (200, 400):
            return AckKind.NO_DATA
        return AckKind.UNKNOWN


def _acknowledgement(body: bytes | str) -> _Acknowledgement | None:
    root = _root(body)
    if root is None or _local(root.tag) != "Acknowledgement_MarketDocument":
        return None
    return _Acknowledgement(
        reasons=tuple((_text(reason, "code"), _text(reason, "text")) for reason in _children(root, "Reason"))
    )


def classify_acknowledgement(body: bytes | str, status: int = 200) -> AckKind | None:
    """What an acknowledgement body means at [status]; `None` when the body is not an acknowledgement."""
    ack = _acknowledgement(body)
    return None if ack is None else ack.kind(status)


# --- The publication document --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Point:
    position: int
    amount: float | None  # None: the point states no price


@dataclass(frozen=True, slots=True)
class _Period:
    start: str
    end: str
    resolution: str
    points: tuple[_Point, ...]


@dataclass(frozen=True, slots=True)
class _Series:
    in_domain: str
    out_domain: str
    currency: str
    unit: str
    curve: str
    sequence: str
    periods: tuple[_Period, ...]


@dataclass(frozen=True, slots=True)
class _Publication:
    type: str
    created: str
    series: tuple[_Series, ...]


_INTEGER = re.compile(r"[+-]?[0-9]+")
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_NON_FINITE = re.compile(r"[+-]?(?:inf|infinity|nan)", re.IGNORECASE)


def _not_a44(reason: str) -> EntsoeParseError:
    return EntsoeParseError(f"response is not an A44 document: {reason}")


def _position(text: str) -> int:
    text = text.strip()
    if not text:
        return 0  # no slot number at all, refused as one
    if not _INTEGER.fullmatch(text):
        raise _not_a44("a position is not a whole number")
    return int(text)


def _amount(text: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    if not (_DECIMAL.fullmatch(text) or _NON_FINITE.fullmatch(text)):
        raise _not_a44("a price is not a number")
    return float(text)


def _publication(body: bytes | str) -> _Publication:
    root = _root(body)
    if root is None:
        raise _not_a44("the body is not well-formed XML")
    if _local(root.tag) != "Publication_MarketDocument":
        raise _not_a44("the root element is not Publication_MarketDocument")
    series = []
    for raw in _children(root, "TimeSeries"):
        periods = []
        for period in _children(raw, "Period"):
            points = tuple(
                _Point(position=_position(_text(point, "position")), amount=_amount(_text(point, "price.amount")))
                for point in _children(period, "Point")
            )
            periods.append(
                _Period(
                    start=_text(period, "timeInterval", "start"),
                    end=_text(period, "timeInterval", "end"),
                    resolution=_text(period, "resolution"),
                    points=points,
                )
            )
        series.append(
            _Series(
                in_domain=_text(raw, "in_Domain.mRID").strip(),
                out_domain=_text(raw, "out_Domain.mRID").strip(),
                currency=_text(raw, "currency_Unit.name"),
                unit=_text(raw, "price_Measure_Unit.name"),
                curve=_text(raw, "curveType"),
                sequence=_text(raw, "classificationSequence_AttributeInstanceComponent.position"),
                periods=tuple(periods),
            )
        )
    return _Publication(type=_text(root, "type"), created=_text(root, "createdDateTime"), series=tuple(series))


# --- Time ----------------------------------------------------------------------------------------------------

_MINUTE_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})Z")
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


def parse_time(value: str) -> datetime:
    """A platform timestamp, as a UTC instant: `2026-09-18T22:00Z`, or RFC 3339 with seconds."""
    value = value.strip()
    if not value:
        raise EntsoeParseError("no timestamp")
    try:
        if match := _MINUTE_STAMP.fullmatch(value):
            year, month, day, hour, minute = (int(part) for part in match.groups())
            return datetime(year, month, day, hour, minute, tzinfo=_UTC)
        if _RFC3339.fullmatch(value):
            return datetime.fromisoformat(value).astimezone(_UTC)
    except ValueError:
        pass
    raise EntsoeParseError(f"{_quote(value)} is not a UTC timestamp")


def _stamp(at: datetime) -> str:
    return at.astimezone(_UTC).isoformat().replace("+00:00", "Z")


def _duration(span: timedelta) -> str:
    """A span the way the relay's messages write one (Go's `1h0m0s`, `15m0s`, `30s`)."""
    seconds = int(span.total_seconds())
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{sign}{hours}h{minutes}m{seconds}s"
    if minutes:
        return f"{sign}{minutes}m{seconds}s"
    return f"{sign}{seconds}s"


_QUARTER = timedelta(minutes=15)
_HALF_HOUR = timedelta(minutes=30)
_HOUR = timedelta(hours=1)


def _resolution(value: str, half_hourly: bool) -> timedelta:
    """PT60M and PT15M for every zone; PT30M only for a half-hourly market, never rounded into a neighbour."""
    value = value.strip()
    if value == "PT60M":
        return _HOUR
    if value == "PT15M":
        return _QUARTER
    if value == "PT30M":
        if half_hourly:
            return _HALF_HOUR
        raise EntsoeParseError(
            f"resolution {_quote(value)} is not this zone's: only a half-hourly market's documents may be PT30M"
        )
    raise EntsoeParseError(f"resolution {_quote(value)} is neither PT60M nor PT15M")


def _allowed_step(step: timedelta, half_hourly: bool) -> bool:
    return step in (_QUARTER, _HOUR) or (half_hourly and step == _HALF_HOUR)


# --- Rows ----------------------------------------------------------------------------------------------------

#: One slot: its UTC start and its price in EUR/MWh.
type _Row = tuple[datetime, float]


def _period_rows(period: _Period, curve: str, step: timedelta, series: int, index: int) -> list[_Row]:
    """One period expanded to a row per slot, by its curve type (A01 every slot, A03 carried forward)."""
    where = f"time series {series} period {index}"
    try:
        period_start = parse_time(period.start)
    except EntsoeParseError as err:
        raise EntsoeParseError(f"{where} start: {err}") from None
    try:
        period_end = parse_time(period.end)
    except EntsoeParseError as err:
        raise EntsoeParseError(f"{where} end: {err}") from None
    # A whole number of slots, or refused: 65 minutes at PT15M must not become four slots.
    length = period_end - period_start
    if length <= timedelta(0):
        raise EntsoeParseError(f"{where} ends at or before it starts ({_duration(length)})")
    if length % step:
        raise EntsoeParseError(
            f"{where} covers {_duration(length)}, which is not a whole number of {_duration(step)} slots"
        )
    slots = length // step
    if not period.points:
        raise EntsoeParseError(f"{where} has no Point")

    stated: dict[int, float] = {}
    for point in period.points:
        if point.position < 1:
            raise EntsoeParseError(f"{where} has position {point.position}, which is not a slot number")
        if point.position > slots:
            raise EntsoeParseError(f"{where} has position {point.position}, beyond its own {slots} slots")
        if point.position in stated:
            raise EntsoeParseError(f"{where} repeats position {point.position}")
        if point.amount is None:
            raise EntsoeParseError(f"{where} position {point.position} has no price")
        if not math.isfinite(point.amount):
            raise EntsoeParseError(f"{where} position {point.position} has a price that is not a finite number")
        stated[point.position] = point.amount

    positions = sorted(stated)
    if curve == _CURVE_STEPWISE:
        if len(positions) != slots:
            raise EntsoeParseError(f"{where} is curveType A01 and states {len(positions)} of its {slots} slots")
        for total, position in enumerate(positions):
            if position != total + 1:
                raise EntsoeParseError(f"{where} is curveType A01 and jumps from slot {total} to {position}")
    elif curve == _CURVE_VARIABLE:
        if positions[0] != 1:
            raise EntsoeParseError(
                f"{where} is curveType A03 and its first block is at slot {positions[0]}, "
                "so the period's start has no value"
            )
    else:
        raise EntsoeParseError(f"{where} has curveType {_quote(curve)}, which this project does not interpret")

    rows: list[_Row] = []
    for i, position in enumerate(positions):
        following = positions[i + 1] if i + 1 < len(positions) else slots + 1
        rows.extend((period_start + step * (slot - 1), stated[position]) for slot in range(position, following))
    return rows


def _series_rows(
    series: _Series,
    curve: str,
    day_start: datetime,
    day_end: datetime,
    index: int,
    half_hourly: bool,
    fine: timedelta | None,
) -> list[_Row]:
    """One series' rows inside the requested day. Periods of other days are skipped; a period is expanded
    whole before clipping, so an A03 block that starts before midnight carries into the day. On a day at two
    resolutions a coarse slot is repeated for each fine slot it holds (exact: an hour's price is each of its
    quarters' price)."""
    if not series.periods:
        raise EntsoeParseError(f"time series {index} has no Period")
    rows: list[_Row] = []
    for i, period in enumerate(series.periods):
        where = f"time series {index} period {i}"
        try:
            period_start = parse_time(period.start)
        except EntsoeParseError as err:
            raise EntsoeParseError(f"{where} start: {err}") from None
        try:
            period_end = parse_time(period.end)
        except EntsoeParseError as err:
            raise EntsoeParseError(f"{where} end: {err}") from None
        if not (period_start < day_end and period_end > day_start):
            continue  # another day's period in the same document
        try:
            step = _resolution(period.resolution, half_hourly)
        except EntsoeParseError as err:
            raise EntsoeParseError(f"{where}: {err}") from None
        expanded = _period_rows(period, curve, step, index, i)
        if fine is not None and step != fine:
            if step % fine or (period_start - _EPOCH) % step:
                raise EntsoeParseError(
                    f"{where} is {period.resolution.strip()} and starts at {_stamp(period_start)}, "
                    f"which cannot be split into {_duration(fine)} slots"
                )
            parts = step // fine
            expanded = [(start + fine * part, price) for start, price in expanded for part in range(parts)]
        rows.extend(row for row in expanded if day_start <= row[0] < day_end)
    return rows


def _mixed_step(
    document: _Publication, area: EntsoeArea, day_start: datetime, day_end: datetime
) -> timedelta | None:
    """The finest resolution of a day published at more than one, `None` for a day at one.

    Real: SDAC moved to 15-minute prices from the CET delivery day 2025-10-01. A local day on Eastern
    European time starts an hour earlier, so its first hour was published PT60M and the rest PT15M. Only
    periods overlapping the day count, and periods at different resolutions must not overlap each other.
    """

    @dataclass(frozen=True, slots=True)
    class Span:
        start: datetime
        end: datetime
        step: timedelta
        series: int
        period: int

    by_sequence: dict[str, list[Span]] = {}
    steps: set[timedelta] = set()
    for i, series in enumerate(document.series):
        if series.in_domain != area.eic or series.out_domain != area.eic:
            continue
        sequence = series.sequence.strip()
        for j, period in enumerate(series.periods):
            # A period that cannot be read is refused with its own message later; here it is no mix.
            try:
                period_start = parse_time(period.start)
                period_end = parse_time(period.end)
                step = _resolution(period.resolution, area.half_hourly)
            except EntsoeParseError:
                continue
            if not (period_start < day_end and period_end > day_start):
                continue
            steps.add(step)
            by_sequence.setdefault(sequence, []).append(
                Span(max(period_start, day_start), min(period_end, day_end), step, i, j)
            )
    if len(steps) < 2:
        return None
    for spans in by_sequence.values():
        for a, x in enumerate(spans):
            for y in spans[a + 1 :]:
                if x.step == y.step or not (x.start < y.end and y.start < x.end):
                    continue
                raise EntsoeParseError(
                    f"time series {x.series} period {x.period} ({_duration(x.step)}) and time series {y.series} "
                    f"period {y.period} ({_duration(y.step)}) both cover {_stamp(max(x.start, y.start))}: "
                    "the day has an overlapping period"
                )
    return min(steps)


def _merge(rows: list[_Row]) -> list[_Row]:
    """Rows in instant order, values for one instant that agree collapsed; ones that disagree refused."""
    merged: list[_Row] = []
    for start, price in sorted(rows, key=lambda row: row[0]):
        if merged and merged[-1][0] == start:
            if abs(merged[-1][1] - price) > _AGREEMENT_EUR_MWH:
                raise EntsoeParseError(
                    f"two values for {_stamp(start)} disagree: {merged[-1][1]:.2f} and {price:.2f} EUR/MWh"
                )
            continue
        merged.append((start, price))
    return merged


def _coverage(rows: list[_Row], day_start: datetime, day_end: datetime, half_hourly: bool) -> None:
    """The rows are the requested day and nothing else: from its start to its end, contiguous, one step."""
    if len(rows) < 2:
        raise EntsoeParseError(f"a day needs at least two rows to state a resolution, got {len(rows)}")
    first = rows[0][0]
    if first != day_start:
        raise EntsoeParseError(f"the rows start at {_stamp(first)}, not at the requested {_stamp(day_start)}")
    step = rows[1][0] - first
    if not _allowed_step(step, half_hourly):
        allowed = "15, 30 or 60 minutes" if half_hourly else "15 or 60 minutes"
        raise EntsoeParseError(f"resolution {_duration(step)} is not {allowed}")
    for i, (start, _) in enumerate(rows):
        want = first + step * i
        if start != want:
            raise EntsoeParseError(
                f"row {i} starts at {_stamp(start)}, expected {_stamp(want)}: "
                "the day has a gap, a repeat or an overlapping period"
            )
    end = rows[-1][0] + step
    if end != day_end:
        raise EntsoeParseError(f"the rows end at {_stamp(end)}, not at the requested {_stamp(day_end)}")


def _unpublished_prefix(
    groups: Mapping[str, list[_Row]], day_start: datetime, day_end: datetime, half_hourly: bool
) -> bool:
    """One sequence, empty or a well-formed strict leading part of the day: the platform still publishing.

    Anything else (a gap, a shifted start, a row at or after the end, two sequences) stays a refusal.
    """
    if len(groups) != 1:
        return False
    for rows in groups.values():
        try:
            merged = _merge(rows)
        except EntsoeParseError:
            return False
        if not merged:
            continue
        if merged[0][0] != day_start or not merged[-1][0] < day_end:
            return False
        if len(merged) == 1:
            continue
        step = merged[1][0] - merged[0][0]
        if not _allowed_step(step, half_hourly):
            return False
        if any(start != day_start + step * i for i, (start, _) in enumerate(merged)):
            return False
        if not merged[-1][0] + step < day_end:
            return False
    return True


def _label(sequence: str) -> str:
    return sequence.strip() or "none"


def _labels(order: Sequence[str]) -> str:
    return ", ".join(_label(sequence) for sequence in order)


def _agree(a: list[_Row], b: list[_Row]) -> str | None:
    """Why two sequences differ, or `None` when they say the same thing slot for slot."""
    if len(a) != len(b):
        return f"one has {len(a)} slots and the other {len(b)}"
    for i, ((a_start, a_price), (b_start, b_price)) in enumerate(zip(a, b)):
        if a_start != b_start:
            return f"slot {i} is {_stamp(a_start)} in one and {_stamp(b_start)} in the other"
        if abs(a_price - b_price) > _AGREEMENT_EUR_MWH:
            return f"slot {i} is {a_price:.2f} EUR/MWh in one and {b_price:.2f} in the other"
    return None


def _choose_sequence(
    groups: Mapping[str, list[_Row]],
    order: Sequence[str],
    day_start: datetime,
    day_end: datetime,
    area: EntsoeArea,
    day: date,
) -> list[_Row]:
    """The day's rows when the area names no sequence. One complete sequence is the answer; several are
    published only when every one is complete and they agree; anything else is refused, never guessed."""
    complete: list[tuple[str, list[_Row]]] = []
    problems: list[str] = []
    for sequence in order:
        try:
            merged = _merge(groups[sequence])
            _coverage(merged, day_start, day_end, area.half_hourly)
        except EntsoeParseError as err:
            problems.append(f"{_label(sequence)} ({err})")
            continue
        complete.append((_label(sequence), merged))

    if not complete:
        raise EntsoeParseError(f"no price sequence for {area.eic} covers {day.isoformat()}: {'; '.join(problems)}")
    if len(complete) != len(order):
        raise EntsoeParseError(
            f"not every sequence for {area.eic} covers {day.isoformat()} ({'; '.join(problems)}), "
            "and without a sequence policy this project cannot show which sequence is the product"
        )
    first_label, first_rows = complete[0]
    for label, rows in complete[1:]:
        if (why := _agree(first_rows, rows)) is not None:
            raise EntsoeParseError(
                f"sequences {first_label} and {label} for {area.eic} are both complete and disagree ({why}), "
                "and without a sequence policy this project cannot choose between them"
            )
    return first_rows


def _sequence_by_policy(
    groups: Mapping[str, list[_Row]],
    order: Sequence[str],
    policy: str,
    day_start: datetime,
    day_end: datetime,
    area: EntsoeArea,
    day: date,
) -> list[_Row]:
    """The day's rows from the one sequence the area names, and from no other. A label the zone does not
    expect refuses the day by name: the platform documents no meaning for these labels, so a new one is a
    change to review, not noise to skip. The named sequence itself is always expected."""
    expected = [label.strip() for label in area.expected_sequences]
    if policy not in expected:
        expected = [policy, *expected]
    for label in order:
        if label != policy and label not in expected:
            raise EntsoeParseError(
                f"the document for {area.eic} carries sequence {_label(label)}, which {area.area_id} does not "
                f"expect (it publishes {policy} and expects {_labels(expected)}); refusing the day rather than "
                "ignoring a label the platform does not document"
            )
    if policy not in groups:
        raise EntsoeParseError(
            f"the document carries no sequence {policy} for {area.eic} (it has {_labels(order)}), "
            f"and {area.area_id} is configured to use only that sequence"
        )
    try:
        merged = _merge(groups[policy])
    except EntsoeParseError as err:
        raise EntsoeParseError(f"sequence {policy} for {area.eic}: {err}") from None
    try:
        _coverage(merged, day_start, day_end, area.half_hourly)
    except EntsoeParseError as err:
        raise EntsoeParseError(f"sequence {policy} for {area.eic} does not cover {day.isoformat()}: {err}") from None
    return merged


# --- The day -------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntsoeDay:
    """One market day as the platform published it: whole, contiguous, at one resolution, in EUR/MWh."""

    area_id: str
    market_day: date
    market_tz: str
    start: datetime  # the market day's first instant, UTC
    resolution_minutes: int
    prices_eur_mwh: tuple[float, ...]
    published: datetime | None  # the document's createdDateTime, absent when unreadable
    source: str = SOURCE_NAME


def parse_day(body: bytes | str, area: EntsoeArea, day: date) -> EntsoeDay:
    """One response body as [area]'s market [day], or a refusal.

    Raises [EntsoeNoData] for a recognized no-data acknowledgement, an answer with no rows for the day or
    a strict leading part of it (the platform still publishing), and [EntsoeParseError] for anything that
    is not that day exactly.
    """
    # Only a body, no status: the no-data rule at the status such an answer normally arrives with.
    ack = _acknowledgement(body)
    if ack is not None and ack.kind(200) is AckKind.NO_DATA:
        raise EntsoeNoData()
    document = _publication(body)
    if (kind := document.type.strip()) != "A44":
        raise EntsoeParseError(f"document type {_quote(kind)} is not A44")
    if not document.series:
        raise EntsoeParseError("the document has no TimeSeries")

    day_start, day_end = market_day_bounds(day, area.market_tz)
    fine = _mixed_step(document, area, day_start, day_end)
    # Grouped by the platform's own sequence label: two sequences are two answers, not one answer twice.
    groups: dict[str, list[_Row]] = {}
    others = 0
    for i, series in enumerate(document.series):
        # A response may carry other zones and other days: this zone's day is selected out of it.
        if series.in_domain != area.eic or series.out_domain != area.eic:
            others += 1
            continue
        if (currency := series.currency.strip()) != _CURRENCY:
            raise EntsoeParseError(f'time series {i} is priced in {_quote(currency)}, not "{_CURRENCY}"')
        # `MWH` in day-ahead documents, `MWh` in others: compared without case.
        if series.unit.strip().lower() != _PRICE_UNIT:
            raise EntsoeParseError(f"time series {i} measures {_quote(series.unit)}, not MWh")
        rows = _series_rows(series, series.curve.strip(), day_start, day_end, i, area.half_hourly, fine)
        groups.setdefault(series.sequence.strip(), []).extend(rows)
    if not groups:
        raise EntsoeParseError(f"the document carries no series for {area.eic} ({others} for other zones)")
    order = sorted(groups)

    if _unpublished_prefix(groups, day_start, day_end, area.half_hourly):
        raise EntsoeNoData()

    if policy := (area.sequence or "").strip():
        rows = _sequence_by_policy(groups, order, policy, day_start, day_end, area, day)
    else:
        rows = _choose_sequence(groups, order, day_start, day_end, area, day)

    try:
        published: datetime | None = parse_time(document.created)
    except EntsoeParseError:
        published = None  # an invented publication time would be worse than none
    step = rows[1][0] - rows[0][0]
    return EntsoeDay(
        area_id=area.area_id,
        market_day=day,
        market_tz=area.market_tz,
        start=rows[0][0],
        resolution_minutes=int(step.total_seconds()) // 60,
        prices_eur_mwh=tuple(price for _, price in rows),
        published=published,
    )


def day_document_fields(day: EntsoeDay) -> dict[str, Any]:
    """The fields of a `"v":1` day document this day states: EUR/kWh (unrounded; the writer rounds), the
    market calendar and the source. The provider adds the display zone, the exchange rate and `retrieved`."""
    return {
        "area_id": day.area_id,
        "market_day": day.market_day,
        "market_tz": day.market_tz,
        "start": day.start.astimezone(time_zones.require_zone(day.market_tz)),
        "res": day.resolution_minutes,
        "prices_eur_kwh": tuple(price / _PER_KWH for price in day.prices_eur_mwh),
        "src": day.source,
        "published": day.published,
    }


# --- The response --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntsoeAnswer:
    """What one response means. `message` is fixed labels, codes and field names: never a body or a URL."""

    outcome: Literal["fetched", "not_published", "failed"]
    day: EntsoeDay | None = None
    code: EntsoeCode | None = None
    message: str = ""
    retry_after: timedelta | None = None


def _status(status: int) -> str:
    try:
        return f"{status} {HTTPStatus(status).phrase}"
    except ValueError:
        return str(status)


def retry_after_delay(header: str | None, now: datetime) -> timedelta:
    """A `Retry-After` hint (seconds or an HTTP date), bounded by [MAX_BACKOFF]; a hint that is missing,
    unreadable or already past is [RATE_LIMITED_BACKOFF]."""
    value = (header or "").strip()
    wait = RATE_LIMITED_BACKOFF
    if _INTEGER.fullmatch(value):
        seconds = int(value)
        if seconds > 0:
            wait = timedelta(seconds=seconds)
    elif value:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=_UTC)
            if when > now:
                wait = when - now
    return min(wait, MAX_BACKOFF)


def read_response(
    status: int,
    body: bytes | str,
    area: EntsoeArea,
    day: date,
    *,
    now: datetime,
    retry_after: str | None = None,
    token: EntsoeToken | None = None,
) -> EntsoeAnswer:
    """One HTTP answer for [area]'s market [day], as the relay's client maps it.

    The order is the rule: a rate limit first (its `Retry-After` read against [now]); then a rejected
    token, before any body is looked at, so it can never read as "no data"; then a server error; then an
    acknowledgement, by its text; then any other status; and only then the document.
    """

    def failed(code: EntsoeCode, message: str, wait: timedelta | None = None) -> EntsoeAnswer:
        if token is not None:
            message = token.redact(message)
        return EntsoeAnswer(outcome="failed", code=code, message=message, retry_after=wait)

    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return failed(
            "rate_limited",
            f"entsoe is rate limiting this token ({_status(status)}); waiting before asking again",
            retry_after_delay(retry_after, now),
        )
    if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        return failed("entsoe_auth", f"entsoe rejected the security token ({_status(status)})")
    if status >= 500:
        return failed("entsoe_unavailable", f"entsoe answered {_status(status)}", SERVER_BACKOFF)

    if (ack := _acknowledgement(body)) is not None:
        # Code 999 is generic, so the text decides; only a fixed label and the code reach the message.
        kind = ack.kind(status)
        code = ack.reason_code()
        if kind is AckKind.NO_DATA:
            return EntsoeAnswer(
                outcome="not_published", code="not_published", message=f"entsoe acknowledgement {code}: {kind}"
            )
        message = f"entsoe answered {_status(status)} with reason {code}: {kind}"
        if kind is AckKind.AUTH:
            return failed("entsoe_auth", message)
        if kind is AckKind.RATE_LIMIT:
            return failed("rate_limited", message, RATE_LIMITED_BACKOFF)
        return failed("entsoe_rejected", message)
    if not 200 <= status <= 299:
        return failed("entsoe_rejected", f"upstream answered {_status(status)}")

    try:
        parsed = parse_day(body, area, day)
    except EntsoeNoData as err:
        return EntsoeAnswer(outcome="not_published", code="not_published", message=str(err))
    except EntsoeParseError as err:
        return failed("provider_refused", f"entsoe: {err}")
    return EntsoeAnswer(outcome="fetched", day=parsed)
