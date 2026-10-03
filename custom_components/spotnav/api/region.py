"""Find a Great Britain price region from a postcode: `spotnav/find_region`.

A Great Britain region (`GB-A` … `GB-P`) is a GSP group, and few people know which one they are in.
Octopus Energy answers a postcode with it (`/v1/industry/grid-supply-points/?postcode=…`, public and
keyless). Home Assistant asks Octopus directly: a postcode is personal data, so it never goes to the
SpotNav relay, and it is neither stored nor logged here (a failure is logged by its kind only).

The answer is the relay's area ids for the groups Octopus names, kept only when the held catalogue lists
them: `region` when there is exactly one, `regions` always. Nothing is chosen on the person's behalf; the
card selects the region in its picker and the person saves it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Final

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..runtime import domain_data
from .common import send_unsupported_version


_LOGGER = logging.getLogger(__name__)

#: Contract version, independent of every other command's.
REGION_API_VERSION: Final = 1

#: Octopus Energy's public grid-supply-point lookup (no key, no account).
OCTOPUS_GSP_URL: Final = "https://api.octopus.energy/v1/industry/grid-supply-points/"

REQUEST_TIMEOUT_S: Final = 10.0
MAX_RESPONSE_BYTES: Final = 64 * 1024

#: A UK postcode, loosely: an outward code of two to four characters and an inward code of three, with
#: or without the space. Anything else is refused before any request (`invalid_postcode`).
POSTCODE: Final = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]? ?[0-9][A-Z]{2}$")

#: A GSP group as Octopus names it (`_C`), and the relay's area id for it (`GB-C`).
GROUP_ID: Final = re.compile(r"^_([A-HJ-NP])$")

REASON_INVALID: Final = "invalid_postcode"
REASON_NOT_FOUND: Final = "not_found"
REASON_UNAVAILABLE: Final = "unavailable"


def normalized_postcode(raw: Any) -> str | None:
    """The postcode in upper case with single spacing, or `None` when it cannot be one."""
    if not isinstance(raw, str):
        return None
    compact = re.sub(r"\s+", "", raw).upper()
    if len(compact) < 5 or len(compact) > 7:
        return None
    spaced = f"{compact[:-3]} {compact[-3:]}"
    return spaced if POSTCODE.match(spaced) else None


def regions_from(document: Any) -> list[str]:
    """The relay area ids (`GB-<letter>`) for the GSP groups an Octopus answer names, each once, in order."""
    if not isinstance(document, dict) or not isinstance(document.get("results"), list):
        raise ValueError("not a grid-supply-points answer")
    regions: list[str] = []
    for row in document["results"]:
        group = row.get("group_id") if isinstance(row, dict) else None
        match = GROUP_ID.match(group) if isinstance(group, str) else None
        if match is not None:
            area_id = f"GB-{match.group(1)}"
            if area_id not in regions:
                regions.append(area_id)
    return regions


async def async_lookup_regions(hass: HomeAssistant, postcode: str) -> list[str]:
    """Ask Octopus which GSP groups a postcode is in. Raises on any transport or format failure."""
    session = async_get_clientsession(hass)
    async with asyncio.timeout(REQUEST_TIMEOUT_S):
        async with session.get(OCTOPUS_GSP_URL, params={"postcode": postcode.replace(" ", "")}) as response:
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}")
            stream = response.content
            chunks: list[bytes] = []
            total = 0
            while chunk := await stream.read(MAX_RESPONSE_BYTES + 1 - total):
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ValueError("too large")
                chunks.append(chunk)
    return regions_from(json.loads(b"".join(chunks).decode("utf-8")))


def _answer(reason: str | None, regions: list[str]) -> dict[str, Any]:
    return {
        "api_version": REGION_API_VERSION,
        "reason": reason,
        "region": regions[0] if len(regions) == 1 else None,
        "regions": regions,
    }


async def async_find_region(hass: HomeAssistant, raw_postcode: Any) -> dict[str, Any]:
    """The whole command, without the transport: validate, ask, and keep what the catalogue lists."""
    postcode = normalized_postcode(raw_postcode)
    if postcode is None:
        return _answer(REASON_INVALID, [])
    try:
        found = await async_lookup_regions(hass, postcode)
    except (TimeoutError, ClientError, ValueError, UnicodeDecodeError) as err:
        # The kind of failure only: the postcode and the answer stay out of the log.
        _LOGGER.warning("Looking up a Great Britain region failed: %s", type(err).__name__)
        return _answer(REASON_UNAVAILABLE, [])
    repository = domain_data(hass).price_repository
    catalogue = None if repository is None else repository.catalogue_snapshot().catalogue
    listed = [area for area in found if catalogue is not None and catalogue.area(area) is not None]
    return _answer(None if listed else REASON_NOT_FOUND, listed)


@websocket_api.websocket_command(
    {
        "type": "spotnav/find_region",
        vol.Optional("api_version"): object,
        "postcode": str,
    }
)
@websocket_api.async_response
async def websocket_find_region(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Find the Great Britain region of a postcode. Every authenticated user may ask; nothing is written."""
    version = msg.get("api_version")
    if type(version) is not int or version != REGION_API_VERSION:
        send_unsupported_version(connection, msg, REGION_API_VERSION)
        return
    connection.send_result(msg["id"], await async_find_region(hass, msg["postcode"]))


@callback
def async_setup_region_api(hass: HomeAssistant) -> None:
    """Register the region lookup once for the domain."""
    websocket_api.async_register_command(hass, websocket_find_region)
