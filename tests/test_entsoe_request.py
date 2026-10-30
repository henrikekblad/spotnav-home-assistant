"""The ENTSO-E request and response without the network: the URL, the token's redaction and what each answer
means. A port of the relay's `entsoe_test.go` (request shape), `entsoe_http_test.go` (statuses and
acknowledgements), `entsoe_redact_test.go` and `token_scan_test.go`, on the parts that are not transport.

What stays with the provider that owns the HTTP client (S9b): the limiter's spacing, timeouts, the
User-Agent and that no `If-Modified-Since` is sent. Here a "response" is a status, a body and a
`Retry-After` value handed to `read_response`, and a "transport error" is an exception built the way an
HTTP client builds one, with the whole URL in its text.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import pickle
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from custom_components.spotnav.pricing.entsoe_parse import (
    AckKind,
    build_request,
    classify_acknowledgement,
    EntsoeArea,
    EntsoeRequestError,
    EntsoeToken,
    EntsoeTransportError,
    loggable_url,
    MAX_BACKOFF,
    RATE_LIMITED_BACKOFF,
    read_response,
    redact_error,
    retry_after_delay,
    SERVER_BACKOFF,
)

from .entsoe_helpers import area, d, DE_LU_EIC, entsoe_body, fixture, IE_SEM, local_area, SECRET, with_sequence

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def token() -> EntsoeToken:
    return EntsoeToken(SECRET)


def query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


def wire(area_id: str | EntsoeArea, day: str) -> dict[str, str]:
    target = area(area_id) if isinstance(area_id, str) else area_id
    return query(build_request(token(), target, d(day)).wire_url())


def answer(status: int, body: str | bytes = "", *, retry_after: str | None = None, area_id: str = "DE-LU",
           day: str = "2026-09-19"):  # fmt: skip
    return read_response(status, body, area(area_id), d(day), now=NOW, retry_after=retry_after, token=token())


# --- entsoe_test.go: the request -----------------------------------------------------------------------------


def test_the_request_shape_is_the_documented_one() -> None:
    target = area("DE-LU")
    loggable = loggable_url(target, d("2026-09-19"))
    assert loggable.startswith("https://web-api.tp.entsoe.eu/api?")
    for want in (
        "documentType=A44",
        "periodStart=202609182200",
        "periodEnd=202609192200",
        "in_Domain=10Y1001A1001A82H",
        "out_Domain=10Y1001A1001A82H",
        "securityToken=%3Credacted%3E",
    ):
        assert want in loggable, loggable
    assert SECRET not in loggable

    request = build_request(token(), target, d("2026-09-19"))
    sent = urlsplit(request.wire_url())
    parameters = query(request.wire_url())
    assert parameters["securityToken"] == SECRET
    assert parameters["documentType"] == "A44"
    assert parameters["contract_MarketAgreement.type"] == "A01"
    assert parameters["in_Domain"] == DE_LU_EIC
    assert (sent.scheme, sent.netloc, sent.path) == ("https", "web-api.tp.entsoe.eu", "/api")
    # The two differ by the token's value and nothing else.
    assert request.wire_url().replace(SECRET, "%3Credacted%3E") == loggable == request.loggable_url


#: Every bundled ENTSO-E area is asked for the CET market day: the relay asked the Eastern European zones for
#: their own day (EE 202609182100); Home Assistant asks for the CET one, the day Nord Pool answers.
CET_INTERVALS = (
    ("2026-09-19", "202609182200", "202609192200"),
    ("2026-03-29", "202603282300", "202603292200"),
    ("2025-10-26", "202510252200", "202510262300"),
)


@pytest.mark.parametrize(
    "area_id",
    [
        "DE-LU", "PL", "EE", "LV", "LT", "ES", "PT", "BE", "FR", "CH", "CZ", "HR", "HU", "SI", "SK", "IT-NORTH",
        "IT-CNORTH", "IT-CSOUTH", "IT-SOUTH", "IT-CALABRIA", "IT-SICILY", "IT-SARDINIA", "BG", "GR", "RO", "FI",
        "AT", "NL", "SE1", "SE2", "SE3", "SE4", "NO1", "NO2", "NO3", "NO4", "NO5", "DK1", "DK2",
    ],
)  # fmt: skip
def test_the_interval_asked_for_is_the_cet_market_day(area_id: str) -> None:
    for day, start, end in CET_INTERVALS:
        parameters = wire(area_id, day)
        assert (parameters["periodStart"], parameters["periodEnd"]) == (start, end), (area_id, day)


@pytest.mark.parametrize(
    ("area_id", "day", "start", "end"),
    [
        ("EE", "2026-09-19", "202609182100", "202609192100"),
        ("BG", "2026-03-29", "202603282200", "202603292100"),
        ("RO", "2025-10-26", "202510252100", "202510262200"),
    ],
)
def test_the_relay_calendar_is_still_expressible(area_id: str, day: str, start: str, end: str) -> None:
    # The relay's own rows of the table: the same builder on an area's local calendar.
    parameters = wire(local_area(area_id), day)
    assert (parameters["periodStart"], parameters["periodEnd"]) == (start, end)


# --- entsoe_sequence_test.go / entsoe_at_ie_test.go: what the request asks for -------------------------------

SEQUENCE_KEY = "classificationSequence_AttributeInstanceComponent.position"


def test_the_configured_sequence_is_asked_for_in_the_query() -> None:
    assert SEQUENCE_KEY not in wire("PL", "2026-09-21")
    assert wire("DE-LU", "2026-09-21")[SEQUENCE_KEY] == "1"


def test_austria_asks_for_sequence_one_and_its_own_day() -> None:
    parameters = wire("AT", "2026-10-02")
    assert parameters == {
        "documentType": "A44",
        "in_Domain": "10YAT-APG------L",
        "out_Domain": "10YAT-APG------L",
        "contract_MarketAgreement.type": "A01",
        "periodStart": "202610012200",
        "periodEnd": "202610022200",
        SEQUENCE_KEY: "1",
        "securityToken": SECRET,
    }


def test_ireland_asks_for_one_sem_trading_day() -> None:
    parameters = wire(IE_SEM, "2026-10-05")
    assert parameters["in_Domain"] == "10Y1001A1001A59C"
    assert (parameters["periodStart"], parameters["periodEnd"]) == ("202610042200", "202610052200")
    assert SEQUENCE_KEY not in parameters
    winter = wire(IE_SEM, "2026-12-01")
    assert (winter["periodStart"], winter["periodEnd"]) == ("202611302300", "202612012300")


# --- entsoe_http_test.go: what an answer means ---------------------------------------------------------------


def test_a_document_is_fetched() -> None:
    result = answer(200, fixture("synthetic_a44_60m.xml"))
    assert result.outcome == "fetched" and result.code is None
    assert len(result.day.prices_eur_mwh) == 24 and result.day.source == "entsoe.eu"


def _status_cases() -> list[tuple[str, int, str, str, str | None, str]]:
    document = fixture("synthetic_a44_60m.xml")
    no_data = fixture("synthetic_ack_999_no_data.xml")
    other_reason = no_data.replace("<code>999</code>", "<code>9999</code>", 1)
    return [
        ("no data with 200", 200, no_data, "not_published", "not_published", "no matching data"),
        ("no data with 400", 400, no_data, "not_published", "not_published", "no matching data"),
        ("another reason code", 400, other_reason, "failed", "entsoe_rejected", "reason 9999"),
        ("a rejected token", 401, no_data, "failed", "entsoe_auth", "security token"),
        ("a forbidden token", 403, no_data, "failed", "entsoe_auth", "security token"),
        ("rate limited", 429, "", "failed", "rate_limited", "rate limiting"),
        ("a server error", 500, "", "failed", "entsoe_unavailable", "500"),
        ("an unavailable server", 503, "", "failed", "entsoe_unavailable", "503"),
        ("an unknown path", 404, "<html>nginx</html>", "failed", "entsoe_rejected", "404"),
        ("an error page with 200", 200, "<html>maintenance</html>", "failed", "provider_refused", "not an A44 document"),
        (
            "another zone's document",
            200,
            document.replace(DE_LU_EIC, "10YPL-AREA-----S", 2),
            "failed",
            "provider_refused",
            "no series for",
        ),
    ]


@pytest.mark.parametrize(
    ("name", "status", "body", "outcome", "code", "want"), _status_cases(), ids=[case[0] for case in _status_cases()]
)
def test_statuses_and_acknowledgements_map_to_outcomes(
    name: str, status: int, body: str, outcome: str, code: str, want: str
) -> None:
    result = answer(status, body)
    assert (result.outcome, result.code) == (outcome, code), result.message
    assert result.day is None
    assert want in result.message
    # The acknowledgement's text is response body, and a body never reaches a message.
    assert "No matching data found" not in result.message


def test_an_empty_token_never_reaches_the_wire() -> None:
    with pytest.raises(EntsoeRequestError, match="no security token"):
        build_request(EntsoeToken("  \n"), area("DE-LU"), d("2026-09-19"))
    assert EntsoeToken("\n").empty and not token().empty


@pytest.mark.parametrize(
    ("header", "want"),
    [
        ("7", timedelta(seconds=7)),
        ("3600", MAX_BACKOFF),
        (format_datetime(NOW + timedelta(seconds=90), usegmt=True), timedelta(seconds=90)),
        (format_datetime(NOW - timedelta(hours=1), usegmt=True), RATE_LIMITED_BACKOFF),
        (None, RATE_LIMITED_BACKOFF),
        ("", RATE_LIMITED_BACKOFF),
        ("soon", RATE_LIMITED_BACKOFF),
        ("0", RATE_LIMITED_BACKOFF),
        ("-5", RATE_LIMITED_BACKOFF),
    ],
)
def test_a_rate_limit_hint_is_honoured_and_bounded(header: str | None, want: timedelta) -> None:
    assert retry_after_delay(header, NOW) == want
    result = answer(429, retry_after=header)
    assert (result.outcome, result.code, result.retry_after) == ("failed", "rate_limited", want)
    assert "rate limiting" in result.message


def test_a_server_error_backs_off_briefly() -> None:
    assert answer(503).retry_after == SERVER_BACKOFF


def test_a_rejected_token_does_not_back_off() -> None:
    # A bad token is a configuration problem for a person to fix, not a rate limit to wait out.
    result = answer(401)
    assert (result.code, result.retry_after) == ("entsoe_auth", None)


def reason_text(body: str) -> str:
    return body[body.index("<text>") + len("<text>") : body.index("</text>")]


NO_DATA_TEXT = "No matching data found for the requested document and interval."
AUTH_TEXT = "The securitytoken is not valid. Please check your credentials."
RATE_TEXT = "Too many requests for this token; slow down."
BAD_TEXT = "Parameter out_Domain is not a valid EIC code."


def _classification_cases() -> list[tuple[str, int, str, str, str]]:
    no_data = fixture("synthetic_ack_999_no_data.xml")
    auth = fixture("synthetic_ack_999_auth.xml")
    unknown = fixture("synthetic_ack_999_unknown.xml")
    rate_limited = no_data.replace(NO_DATA_TEXT, RATE_TEXT, 1)
    bad_request = no_data.replace(NO_DATA_TEXT, BAD_TEXT, 1)
    other_code = no_data.replace("<code>999</code>", "<code>1000</code>", 1)
    return [
        ("no data, 200", 200, no_data, "not_published", "no matching data"),
        ("no data, 400", 400, no_data, "not_published", "no matching data"),
        ("no data, 401", 401, no_data, "entsoe_auth", "security token"),
        ("no data, 500", 500, no_data, "entsoe_unavailable", "500"),
        ("a credential complaint under 999, 200", 200, auth, "entsoe_auth", "authentication"),
        ("a credential complaint under 999, 400", 400, auth, "entsoe_auth", "authentication"),
        ("a rate limit under 999", 400, rate_limited, "rate_limited", "rate limited"),
        ("a bad request under 999", 400, bad_request, "entsoe_rejected", "request rejected"),
        ("an unknown 999", 400, unknown, "entsoe_rejected", "unrecognized"),
        ("a code that is not 999", 400, other_code, "entsoe_rejected", "unrecognized"),
    ]


@pytest.mark.parametrize(
    ("name", "status", "body", "code", "want"), _classification_cases(), ids=[case[0] for case in _classification_cases()]
)
def test_acknowledgement_classification(name: str, status: int, body: str, code: str, want: str) -> None:
    result = answer(status, body)
    assert result.code == code, result.message
    assert result.outcome == ("not_published" if code == "not_published" else "failed")
    assert want in result.message
    # The text is read to classify and never quoted: a body can echo the request, token and all.
    assert reason_text(body) not in result.message
    if code == "not_published":
        assert "no matching data" in result.message


def test_an_acknowledgement_rate_limit_backs_the_token_off() -> None:
    body = fixture("synthetic_ack_999_no_data.xml").replace(NO_DATA_TEXT, RATE_TEXT, 1)
    assert answer(400, body).retry_after == RATE_LIMITED_BACKOFF


def ack(*reasons: tuple[str, str]) -> str:
    rows = "".join(f"\n  <Reason><code>{code}</code><text>{text}</text></Reason>" for code, text in reasons)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">\n'
        "  <mRID>ack</mRID>\n  <createdDateTime>2026-09-19T13:45:12Z</createdDateTime>"
        f"{rows}\n</Acknowledgement_MarketDocument>\n"
    )


MIXED_ACKS = [
    ("auth then no-data", 400, ack(("999", AUTH_TEXT), ("999", NO_DATA_TEXT)), AckKind.AUTH),
    ("no-data then auth", 400, ack(("999", NO_DATA_TEXT), ("999", AUTH_TEXT)), AckKind.AUTH),
    ("auth then no-data with 200", 200, ack(("999", AUTH_TEXT), ("999", NO_DATA_TEXT)), AckKind.AUTH),
    ("rate limit then no-data", 400, ack(("999", RATE_TEXT), ("999", NO_DATA_TEXT)), AckKind.RATE_LIMIT),
    ("no-data then rate limit", 400, ack(("999", NO_DATA_TEXT), ("999", RATE_TEXT)), AckKind.RATE_LIMIT),
    ("bad request then no-data", 400, ack(("999", BAD_TEXT), ("999", NO_DATA_TEXT)), AckKind.BAD_REQUEST),
    ("no-data then bad request", 400, ack(("999", NO_DATA_TEXT), ("999", BAD_TEXT)), AckKind.BAD_REQUEST),
    (
        "no-data and an unrecognized complaint",
        400,
        ack(("999", NO_DATA_TEXT), ("999", "Something else entirely.")),
        AckKind.UNKNOWN,
    ),
    ("no-data twice, both saying it", 400, ack(("999", NO_DATA_TEXT), ("999", "No matching data found.")), AckKind.NO_DATA),
    ("999 with no text at all", 400, ack(("999", "")), AckKind.UNKNOWN),
    ("a mixed code, with a credential complaint", 400, ack(("999", NO_DATA_TEXT), ("1000", AUTH_TEXT)), AckKind.AUTH),
    ("a mixed code, with nothing recognizable", 400, ack(("1000", NO_DATA_TEXT)), AckKind.UNKNOWN),
]


@pytest.mark.parametrize(("name", "status", "body", "kind"), MIXED_ACKS, ids=[case[0] for case in MIXED_ACKS])
def test_a_mixed_acknowledgement_is_never_no_data(name: str, status: int, body: str, kind: AckKind) -> None:
    assert classify_acknowledgement(body, status) is kind
    result = answer(status, body)
    assert (result.outcome == "not_published") == (kind is AckKind.NO_DATA)
    if kind is AckKind.AUTH:
        assert result.code == "entsoe_auth"
    assert str(kind) in result.message
    for text in (AUTH_TEXT, RATE_TEXT, BAD_TEXT, NO_DATA_TEXT, "Something else entirely."):
        assert text not in result.message
    for probe in (SECRET, "securityToken", "web-api.tp.entsoe.eu"):
        assert probe not in result.message


def test_a_publication_is_not_an_acknowledgement() -> None:
    assert classify_acknowledgement(fixture("synthetic_a44_60m.xml")) is None
    assert classify_acknowledgement("") is None
    assert classify_acknowledgement("<html/>") is None


# --- entsoe_test.go: outcomes through the response -----------------------------------------------------------


def test_a_real_no_data_acknowledgement_is_not_published() -> None:
    result = answer(200, fixture("entsoe_SE4_2026-09-24_no_data.xml"), area_id="SE4", day="2026-09-24")
    assert (result.outcome, result.code) == ("not_published", "not_published")


def test_a_pre_publication_prefix_is_not_published() -> None:
    prefix = with_sequence(entsoe_body(DE_LU_EIC, "2026-09-18T22:00Z", "2026-09-18T23:00Z", "PT60M", ["42.00"]), "1")
    assert answer(200, prefix).outcome == "not_published"


def test_a_malformed_partial_day_is_a_failure() -> None:
    broken = entsoe_body(DE_LU_EIC, "2026-09-18T22:00Z", "2026-09-19T00:00Z", "PT60M", ["42.00"])
    assert (answer(200, broken).outcome, answer(200, broken).code) == ("failed", "provider_refused")


# --- entsoe_redact_test.go -----------------------------------------------------------------------------------


def _renderings(value: object) -> list[str]:
    out = [str(value), repr(value), f"{value}", f"{value!r}", "%s" % (value,), "%r" % (value,)]
    try:
        out.append(json.dumps(value, default=str))
    except (TypeError, ValueError):
        pass
    return out


def test_the_token_never_renders() -> None:
    secret = token()
    for rendered in _renderings(secret):
        assert SECRET not in rendered
    assert str(secret) == "<redacted>"
    with pytest.raises(TypeError):
        pickle.dumps(secret)
    with pytest.raises(TypeError):
        copy.deepcopy(secret)
    with pytest.raises(TypeError):
        json.dumps(secret)
    assert not hasattr(secret, "__dict__")


def test_the_request_renders_as_its_loggable_url() -> None:
    request = build_request(token(), area("DE-LU"), d("2026-09-19"))
    for rendered in _renderings(request):
        assert SECRET not in rendered
        assert "%3Credacted%3E" in rendered
    with pytest.raises(TypeError):
        pickle.dumps(request)
    assert SECRET in request.wire_url()


@pytest.mark.parametrize("area_id", ["DE-LU", "PL", "EE", "LV", "LT", "FI", "AT"])
def test_the_loggable_url_carries_the_placeholder_and_never_the_value(area_id: str) -> None:
    for loggable in (loggable_url(area(area_id), d("2026-09-19")), build_request(token(), area(area_id), d("2026-09-19")).loggable_url):
        assert SECRET not in loggable
        assert "securityToken=%3Credacted%3E" in loggable


def _transport_error(request, cause: BaseException | None = None) -> Exception:
    # What an HTTP client produces: the whole URL, token included, in the message and on the object.
    err = OSError(f"Cannot connect to host for GET {request.wire_url()}: connection refused")
    err.url = request.wire_url()
    if cause is not None:
        err.__cause__ = cause
    return err


def _graph(err: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    stack: list[BaseException | None] = [err]
    while stack:
        item = stack.pop()
        if item is None or any(item is other for other in seen):
            continue
        seen.append(item)
        stack += [item.__cause__, item.__context__]
    return seen


def test_a_transport_error_cannot_leak_the_token(caplog: pytest.LogCaptureFixture) -> None:
    request = build_request(token(), area("DE-LU"), d("2026-09-19"))

    def fetch() -> None:
        # The pattern the provider uses: sanitize inside the handler, raise outside it. A raise inside it,
        # even `from None`, would set the new error's __context__ to the original.
        try:
            raise _transport_error(request)
        except OSError as original:
            sanitized = redact_error(request.token, original)
        raise sanitized

    with pytest.raises(EntsoeTransportError) as caught:
        fetch()
    err = caught.value
    message = str(err)
    assert SECRET not in message
    assert "<redacted>" in message
    assert "connection refused" in message and "documentType=A44" in message
    # The cause is gone: the original holds the whole URL, so keeping it reachable would leave the token one
    # attribute away.
    assert err.__cause__ is None and err.__context__ is None and err.__suppress_context__
    for reachable in _graph(err):
        for rendered in (*_renderings(reachable), *(str(value) for value in vars(reachable).values())):
            assert SECRET not in rendered
    # And the line logged from it is clean.
    with caplog.at_level(logging.WARNING):
        logging.getLogger("spotnav.test").warning("fetch failed for %s: %s (%s)", "DE-LU", err, request)
        logging.getLogger("spotnav.test").exception("fetch failed", exc_info=err)
    assert SECRET not in caplog.text
    assert "connection refused" in caplog.text


def test_a_non_2xx_body_that_echoes_the_request_cannot_leak_the_token() -> None:
    request = build_request(token(), area("DE-LU"), d("2026-09-19"))
    result = answer(500, f"<html>echoed-by-the-server {request.wire_url()}</html>")
    assert result.outcome == "failed"
    assert SECRET not in result.message and "echoed-by-the-server" not in result.message
    assert "500" in result.message


def test_a_parse_failure_names_fields_and_never_quotes_the_body() -> None:
    result = answer(200, "<html><body>secret-looking-html</body></html>")
    assert result.code == "provider_refused"
    assert "secret-looking-html" not in result.message


def test_a_timeout_stays_recognisable_without_the_error_that_said_so() -> None:
    request = build_request(token(), area("DE-LU"), d("2026-09-19"))
    timed_out = redact_error(request.token, TimeoutError(f"GET {request.wire_url()} timed out"))
    assert timed_out.timeout and not timed_out.cancelled
    assert SECRET not in str(timed_out)
    wrapped = OSError(f"GET {request.wire_url()}")
    wrapped.__cause__ = asyncio.TimeoutError()
    assert redact_error(request.token, wrapped).timeout
    cancelled = redact_error(request.token, asyncio.CancelledError(f"GET {request.wire_url()}"))
    assert cancelled.cancelled and not cancelled.timeout
    assert SECRET not in str(cancelled)


def _failure_paths() -> list[tuple[str, int, str]]:
    request = build_request(token(), area("DE-LU"), d("2026-09-19"))
    document = fixture("synthetic_a44_60m.xml")
    return [
        ("a body that echoes the request", 500, f"<html>upstream error for {request.wire_url()}</html>"),
        ("an error status", 502, ""),
        ("a body that is not XML", 200, "<html><body>maintenance</body></html>"),
        ("a document for another zone", 200, document.replace(DE_LU_EIC, "10YPL-AREA-----S", 1)),
        ("a rejected token", 401, fixture("synthetic_ack_999_auth.xml")),
        ("an acknowledgement that echoes the request", 400, ack(("999", f"bad request {request.wire_url()}"))),
        ("a document type that echoes the token", 200, document.replace("<type>A44</type>", f"<type>{SECRET}</type>", 1)),
        ("a currency that echoes the token", 200, document.replace(">EUR<", f">{SECRET}<", 1)),
        ("a time stamp that echoes the token", 200, document.replace("2026-09-18T22:00Z</start>", f"{SECRET}</start>")),
    ]


@pytest.mark.parametrize(("name", "status", "body"), _failure_paths(), ids=[case[0] for case in _failure_paths()])
def test_no_failure_path_leaves_a_token_reachable(name: str, status: int, body: str) -> None:
    result = answer(status, body)
    assert result.outcome == "failed", result
    assert result.message
    for rendered in _renderings(result):
        assert SECRET not in rendered
    # Without the token to redact by, the parser's own messages still hold no more than a short field.
    bare = read_response(status, body, area("DE-LU"), d("2026-09-19"), now=NOW)
    assert SECRET[:12] not in bare.message


# --- token_scan_test.go --------------------------------------------------------------------------------------


def test_the_tree_does_not_contain_the_real_token() -> None:
    """The pre-handoff scan, skipped unless a token file is named: the suite stays offline and needs no
    credential. Run it as the last step before a handoff:

        SPOTNAV_ENTSOE_TOKEN_FILE=/path/to/token pytest tests/test_entsoe_request.py -k real_token

    It reports which files contain the token, never the token, its length or a hash of it.
    """
    path = os.environ.get("SPOTNAV_ENTSOE_TOKEN_FILE", "").strip()
    if not path:
        pytest.skip("set SPOTNAV_ENTSOE_TOKEN_FILE to search this tree for the real token")
    secret = Path(path).read_bytes().strip()
    # A value this short would match half the tree and prove nothing.
    assert len(secret) >= 16, "the token file holds too little to scan for"
    root = Path(__file__).resolve().parents[1]
    hits: list[str] = []
    scanned = 0
    for current, directories, files in os.walk(root):
        directories[:] = [name for name in directories if name not in {".git", "node_modules", "dist", ".venv"}]
        for name in files:
            candidate = Path(current, name)
            try:
                if candidate.stat().st_size > 8 << 20:
                    continue
                content = candidate.read_bytes()
            except OSError:
                continue
            scanned += 1
            if secret in content:
                hits.append(str(candidate.relative_to(root)))
    assert not hits, f"the token appears in {len(hits)} of {scanned} files: {hits}"
