"""Send the debug bundle to SpotNav's developer, only when an administrator presses "Send debug info" in the card.

The report is the same redacted bundle the card's "Download debug info" saves (`debug_bundle.async_build_debug_bundle`),
with the administrator's message and, if they gave one, an e-mail address to answer. It goes to the relay's
`POST /v1/support/report` as gzip-compressed JSON:

    {"version": 1, "install": "<sha256 hex>", "message": "...", "email": "..." | null,
     "spotnav_version": "...", "ha_version": "...", "bundle": {...}}

`install` is the SHA-256 of Home Assistant's instance id followed by a fixed SpotNav salt, so the relay can count
one installation's reports without ever being sent the id itself. The relay answers `201 {"ref": "SN-XXXX"}`, or
`400`, `413`, `429 {"retry_after_s": n}` or `503`; each refusal is one stable code here.

Nothing here runs by itself: the only caller is the admin-only `spotnav/send_debug_info` command. The bundle is
built as the download builds it (copied on the loop, redacted in the executor); the JSON encoding and the gzip
run in the executor too, and the POST uses Home Assistant's shared client session.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Final

import aiohttp
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import instance_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.json import json_bytes

from .card_asset import async_manifest_version
from .debug_bundle import async_build_debug_bundle
from .notifications.push import DEFAULT_BASE_URL

_LOGGER = logging.getLogger(__name__)

#: The relay's endpoint and the report's own format version.
REPORT_PATH: Final = "/v1/support/report"
REPORT_VERSION: Final = 1
#: Added to the instance id before it is hashed; fixed, so one installation keeps one `install`.
INSTALL_SALT: Final = "spotnav-support-report-v1"
#: One request, bounded; never retried.
REPORT_TIMEOUT_S: Final = 60.0
#: The relay's limits: the message in characters, the address in characters, the compressed body in bytes.
MAX_MESSAGE_CHARS: Final = 4000
MAX_EMAIL_CHARS: Final = 254
MAX_BODY_BYTES: Final = 8 * 1024 * 1024
#: The `client` block the card adds about itself: these keys only, strings cut to this length.
#: The browser's user agent stays out of a sent report (the download keeps it).
CLIENT_KEYS: Final = ("card_bundle_hash", "card_version", "card_outdated", "note")
MAX_CLIENT_TEXT: Final = 512

#: Stable refusal codes, as the card switches on them.
ERROR_INVALID_MESSAGE: Final = "spotnav_report_invalid_message"
ERROR_INVALID_EMAIL: Final = "spotnav_report_invalid_email"
ERROR_RATE_LIMITED: Final = "spotnav_report_rate_limited"
ERROR_TOO_LARGE: Final = "spotnav_report_too_large"
ERROR_REJECTED: Final = "spotnav_report_rejected"
ERROR_NETWORK: Final = "spotnav_report_network"
ERROR_SERVER: Final = "spotnav_report_server"

_REF = re.compile(r"^SN-[A-Z0-9]{4}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
#: Control characters, except the newline (and the tab, which reads as a space).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@dataclass(frozen=True, slots=True)
class ReportResult:
    """What the relay made of one report: a reference, or a stable code (and the wait when rate limited)."""

    ref: str | None = None
    error: str | None = None
    retry_after_s: int | None = None


def install_hash(raw_instance_id: str) -> str:
    """The installation as the relay knows it: never the instance id itself."""
    return hashlib.sha256(f"{raw_instance_id}{INSTALL_SALT}".encode()).hexdigest()


def clean_message(value: Any) -> str | None:
    """The message as sent (control characters other than newlines removed, `\\r\\n` as `\\n`), or `None` when
    it is empty or longer than the relay takes."""
    if not isinstance(value, str):
        return None
    text = _CONTROL.sub("", value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")).strip()
    if not text or len(text) > MAX_MESSAGE_CHARS:
        return None
    return text


def clean_email(value: Any) -> tuple[bool, str | None]:
    """`(valid, address)`: an absent or blank address is valid and `None`."""
    if value is None:
        return True, None
    if not isinstance(value, str):
        return False, None
    text = value.strip()
    if not text:
        return True, None
    if len(text) > MAX_EMAIL_CHARS or _EMAIL.match(text) is None:
        return False, None
    return True, text


def clean_client(value: Any) -> dict[str, Any] | None:
    """The card's own block (`clientBlock` in the card), kept to its known keys and plain values."""
    if not isinstance(value, dict):
        return None
    block: dict[str, Any] = {}
    for key in CLIENT_KEYS:
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, str):
            block[key] = item[:MAX_CLIENT_TEXT]
        elif item is None or isinstance(item, bool):
            block[key] = item
    return block


def encode_report(payload: dict[str, Any]) -> bytes:
    """The request body: the payload as compact JSON, gzip-compressed. Runs in the executor."""
    return gzip.compress(json_bytes(payload), compresslevel=6)


async def async_send_report(
    hass: HomeAssistant,
    message: str,
    email: str | None,
    client: dict[str, Any] | None = None,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> ReportResult:
    """Build the bundle, send it once, and say what came of it. Never raises for the relay's answer."""
    bundle = await async_build_debug_bundle(hass)
    if client is not None:
        bundle["client"] = client
    version = await async_manifest_version(hass)
    payload = {
        "version": REPORT_VERSION,
        "install": install_hash(await instance_id.async_get(hass)),
        "message": message,
        "email": email,
        "spotnav_version": None if version is None else str(version),
        "ha_version": HA_VERSION,
        "bundle": bundle,
    }
    body = await hass.async_add_executor_job(encode_report, payload)
    if len(body) > MAX_BODY_BYTES:
        _LOGGER.info("The debug report was not sent: %d bytes compressed is more than the server takes", len(body))
        return ReportResult(error=ERROR_TOO_LARGE)
    try:
        async with async_get_clientsession(hass).post(
            f"{base_url}{REPORT_PATH}",
            data=body,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
            timeout=aiohttp.ClientTimeout(total=REPORT_TIMEOUT_S),
        ) as response:
            status = response.status
            answer: Any = None
            try:
                answer = await response.json(content_type=None)
            except (ValueError, aiohttp.ClientError):
                answer = None
            retry_header = response.headers.get("Retry-After")
    except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as err:
        _LOGGER.info("The debug report could not be sent: %s", type(err).__name__)
        return ReportResult(error=ERROR_NETWORK)
    result = _result(status, answer, retry_header)
    if result.ref is not None:
        _LOGGER.info("Sent a debug report (%d bytes compressed), reference %s", len(body), result.ref)
    else:
        _LOGGER.info("The debug report was refused: HTTP %d (%s)", status, result.error)
    return result


def _result(status: int, answer: Any, retry_header: str | None) -> ReportResult:
    """One HTTP answer as a reference or a stable code."""
    record = answer if isinstance(answer, dict) else {}
    if status in (200, 201):
        ref = record.get("ref")
        if isinstance(ref, str) and _REF.match(ref):
            return ReportResult(ref=ref)
        return ReportResult(error=ERROR_SERVER)
    if status == 429:
        return ReportResult(error=ERROR_RATE_LIMITED, retry_after_s=_retry_after(record.get("retry_after_s"), retry_header))
    if status == 413:
        return ReportResult(error=ERROR_TOO_LARGE)
    if status == 400:
        return ReportResult(error=ERROR_REJECTED)
    return ReportResult(error=ERROR_SERVER)


def _retry_after(value: Any, header: str | None) -> int | None:
    """The wait in whole seconds, from the body or else the `Retry-After` header; `None` when neither says."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    if header is not None and header.strip().isdigit():
        return int(header.strip())
    return None
