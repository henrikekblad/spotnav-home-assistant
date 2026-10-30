"""The SpotNav Relay's wire: its area list, index and day files, fetched bounded in time and size.

Moved out of the repository unchanged: the repository keeps what it holds and why, this module only how a
document is asked for. Contract v2: the area list and the index are read from `/v2/…` first and from
`/v1/…` when the relay answers v2 with a 404 or a body this client cannot read ([async_fetch_versioned]).
Day files keep their `/v1/<AREA>/<YYYY>/<MM-DD>.json` paths under both, one per **market** day.

Every zone a fetched document names is resolved off the event loop before anything parses it, and no body
or URL is ever logged.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Final
from urllib.parse import quote

from aiohttp import ClientError, ClientSession
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ... import time_zones
from ..relay_contract import loads_document, RelayParseError
from . import DayFetch, FetchCode, ProviderKey

#: The deployed relay; injected so tests can build a repository that cannot reach the network.
DEFAULT_BASE_URL: Final = "https://spotnav.sensnology.se"

#: Request timeout and body size limit. A day document is ~10 kB and an index ~1 kB; half a
#: megabyte is generous but bounds a hung connection or a mistaken URL.
REQUEST_TIMEOUT_S: Final = 20.0
MAX_RESPONSE_BYTES: Final = 512 * 1024

#: Bytes asked per read: small enough that a trickling stream yields to the event loop and meets
#: the overall timeout.
CHUNK_BYTES: Final = 64 * 1024

#: The contract versions asked for, newest first: a relay that predates v2 answers it with a 404.
CONTRACT_VERSIONS: Final = (2, 1)


class RelayFailure(Exception):
    """A fetch that did not produce a document, carrying its stable code (and the HTTP status, if any)."""

    def __init__(self, code: FetchCode, status: int | None = None) -> None:
        super().__init__(code)
        self.code: FetchCode = code
        self.status = status


async def _read_bounded(stream) -> bytes:
    """Read a body to EOF, refusing to hold more than [MAX_RESPONSE_BYTES] of it.

    One aiohttp `read(n)` may return fewer than `n` bytes short of EOF, so a chunked response would
    otherwise be rejected as malformed JSON. This accumulates until the stream ends and stops as soon as
    the limit would be exceeded (the caller's `async with` abandons the connection). The caller's timeout
    wraps the whole loop.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        # Ask for the rest of the allowance plus one byte to prove an overflow, in bounded slices.
        chunk = await stream.read(min(CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise RelayFailure("too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def day_path(area_id: str, day: date) -> str:
    """A market-day file's path, the same under both contract versions."""
    return f"/v1/{quote(area_id, safe='')}/{day.year:04d}/{day.month:02d}-{day.day:02d}.json"


class RelayProvider:
    """The relay's published files. Holds nothing: every call is one request (or two, for a v2 fallback)."""

    key: ProviderKey = "relay"
    #: What the card and the app call it, and where it is (attribution).
    name: str = "SpotNav relay"
    url: str = "https://spotnav.sensnology.se"
    has_index = True
    #: The area list states each area's publication and calendar; the relay keeps its archive.
    publication = None
    history_reach = None

    def __init__(
        self, hass: HomeAssistant, *, base_url: str = DEFAULT_BASE_URL, session: ClientSession | None = None
    ) -> None:
        self._hass = hass
        self._base_url = base_url.rstrip("/")
        # `None` means the shared HA client when a request is made (`async_get_clientsession`
        # needs a running loop, and setup constructs this before anything is asked).
        self._session = session

    def market_calendar(self, area_id: str) -> str | None:
        """The area list's own `market_tz`."""
        return None

    def wants_again(self, area_id: str, document: Any) -> bool:
        """A relay file is final as published."""
        return False

    async def async_fetch_day(self, area_id: str, market_day: date) -> DayFetch:
        """One market-day file as published (the relay's 404 is a failed request with `http_status`)."""
        try:
            document = await self.async_fetch_json(day_path(area_id, market_day))
        except RelayFailure as err:
            return DayFetch("failed", code=err.code)
        except RelayParseError as err:
            return DayFetch("failed", code=err.code)
        return DayFetch("fetched", document=document)

    async def async_fetch_versioned(self, name: str, parse, versions: tuple[int, ...]) -> tuple[Any, Any]:
        """`/v<n>/<name>` parsed as version n, for the first of [versions] the relay answers readably.

        A 404 or a body this client cannot read moves on to the next version; a transport failure does not
        (an unreachable relay is not an old one). The last version's failure is the one raised.
        """
        for position, version in enumerate(versions):
            last = position == len(versions) - 1
            try:
                document = await self.async_fetch_json(f"/v{version}/{name}")
                return document, parse(document, version=version)
            except RelayFailure as err:
                if last or err.status != 404:
                    raise
            except RelayParseError:
                if last:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def _client(self) -> ClientSession:
        if self._session is None:
            self._session = async_get_clientsession(self._hass)
        return self._session

    async def async_fetch_json(self, path: str) -> Any:
        """One published document, bounded in time and size.

        A non-200 is a failure, not a document; the body must be JSON (a proxy's HTML is `not_json`). It
        is read to EOF in bounded slices under one overall timeout and never logged.
        """
        url = f"{self._base_url}{path}"
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                async with self._client().get(url) as response:
                    if response.status != 200:
                        raise RelayFailure("http_status", response.status)
                    body = await _read_bounded(response.content)
        except TimeoutError as err:
            raise RelayFailure("timeout") from err
        except ClientError as err:
            raise RelayFailure("network") from err
        document = loads_document(body.decode("utf-8", errors="replace"))
        # Every zone the document names, resolved off the loop before any parser reads it.
        await time_zones.async_resolve_document(document)
        return document
