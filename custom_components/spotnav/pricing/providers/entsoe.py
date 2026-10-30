"""Day-ahead prices from ENTSO-E's Transparency Platform, with the installation's own security token.

The request, the answer and the document are `pricing/entsoe_parse.py`'s (a port of the relay's client); this
module is the transport around it:

* **One request per file**, through Home Assistant's shared aiohttp session, with SpotNav's User-Agent, one overall
  timeout and a bounded body ([REQUEST_TIMEOUT_S], `entsoe_parse.MAX_BODY_BYTES`). Requests go one at a time, at
  least [MIN_SPACING_S] apart.
* **The CET market day**, like Nord Pool (`routes.cet_market_tz`): a Finnish display day is cut from two CET files
  by `market_day`, and a switch between the two providers keeps the stored files. The schedule is the authority
  (no index): today and earlier when not held, tomorrow from just before 13:00 Brussels ([PUBLICATION], the time
  the area list states).
* **What an answer means** is `entsoe_parse.read_response`'s: a rate limit (429 or its acknowledgement) and a
  server error make the provider wait the time it names (`Retry-After`, bounded) before it asks anything again,
  answering `rate_limited` / `entsoe_unavailable` meanwhile without a request; a 401/403 (or an acknowledgement
  naming the credentials) is `entsoe_auth` and marks the stored key `rejected`, which takes every ENTSO-E area off
  this provider at once (the router chooses again: the relay, for now); "no matching data" or a day that is still
  being published is `not_published`; a body the parser refuses is `provider_refused`.
* **History reaches years back** ([history_reach] `None`); the backfill that uses it is not here.
* **The rate**: the area's ECB rate, as for Nord Pool (`providers/rates.py`). `src` is `"entsoe.eu"`.

The token is read from the key source on every request and lives only inside an `EntsoeRequest`. Nothing here logs
a URL, a body or an error's text: a transport failure is reduced to its code while the original error is dropped
inside the `except` block (its text may hold the request URL), so no exception, cause or context that leaves this
module can reach the token.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Final, Literal, Protocol

from aiohttp import ClientSession
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from ..area_catalogue import BundledCatalogue
from ..day_document import build_day_document
from ..entsoe_parse import (
    build_request,
    day_document_fields,
    EntsoeArea,
    EntsoeRequest,
    EntsoeRequestError,
    EntsoeToken,
    MAX_BODY_BYTES,
    read_response,
)
from ..fx_rates import USER_AGENT
from ..relay_contract import AreaPublication, DEFAULT_PUBLICATION, PriceDocument
from . import DayFetch, FailureCode, market_today, ProviderKey
from .rates import DayRate, FX_SRC, RateSource

_LOGGER = logging.getLogger(__name__)

#: ENTSO-E publishes the day-ahead results shortly before 13:00 Brussels, the time the area list states.
PUBLICATION: Final = DEFAULT_PUBLICATION

#: One request, bounded: a day document is tens of kilobytes.
REQUEST_TIMEOUT_S: Final = 30.0

#: The least time between two requests. The platform allows 400 a minute per token; SpotNav asks a handful a day.
MIN_SPACING_S: Final = 1.0

#: Bytes asked per read, so a trickling body yields to the loop and meets the overall timeout.
_CHUNK_BYTES: Final = 64 * 1024

#: The area a key is checked against when the area asked about has no ENTSO-E route.
CHECK_AREA: Final = "DE-LU"

#: What checking a key found: the platform accepted it, rejected it, or could not be asked.
KeyCheck = Literal["ok", "rejected", "unreachable"]

#: The stored key's state after an answer that proves it.
_ACCEPTED: Final = "ok"
_REJECTED: Final = "rejected"


class KeySource(Protocol):
    """Where the installation's ENTSO-E key is kept (`pricing/price_store.PriceSourceStore`)."""

    def entsoe_key(self) -> EntsoeToken | None: ...

    @property
    def entsoe_key_usable(self) -> bool: ...

    @property
    def entsoe_key_state(self) -> str | None: ...

    async def async_mark_entsoe_key(self, token: EntsoeToken, state: Literal["ok", "rejected", "unchecked"]) -> bool:
        ...

    def add_entsoe_listener(self, listener: Callable[[], None]) -> Callable[[], None]: ...


class _TooLarge(Exception):
    pass


async def _read_bounded(stream) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(min(_CHUNK_BYTES, MAX_BODY_BYTES + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


class EntsoeProvider:
    """Day files from the Transparency Platform, one request per file, with the stored key."""

    key: ProviderKey = "entsoe"
    has_index = False
    publication: AreaPublication = PUBLICATION
    #: The platform keeps years of day-ahead prices.
    history_reach: timedelta | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        bundled: BundledCatalogue,
        keys: KeySource,
        fx: RateSource | None,
        *,
        session: ClientSession | None = None,
        now: Callable[[], datetime] | None = None,
        min_spacing_s: float = MIN_SPACING_S,
        endpoint: str | None = None,
    ) -> None:
        self._hass = hass
        self._bundled = bundled
        self._keys = keys
        self._session = session
        self._now = now if now is not None else dt_util.utcnow
        self._rate = DayRate(fx, self._now)
        self._min_spacing_s = min_spacing_s
        self._endpoint = endpoint
        # One request at a time, spaced (loop time: the spacing is about the wire, not the planner's clock).
        self._lock = asyncio.Lock()
        self._last_request: float | None = None
        # Nothing is asked before this instant (a rate limit or a server error said to wait), with its code.
        self._quiet_until: datetime | None = None
        self._quiet_code: FailureCode | None = None

    # -------------------------------------------------------------- what the router and repository ask

    def serves(self, area_id: str) -> bool:
        """Whether the platform publishes the area (it has an ENTSO-E route in the bundled catalogue)."""
        routes = self._bundled.routes.get(area_id)
        return routes is not None and routes.entsoe is not None

    def key_usable(self) -> bool:
        """Whether a key is stored that the platform has not rejected."""
        return self._keys.entsoe_key_usable

    def add_key_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        return self._keys.add_entsoe_listener(listener)

    def market_calendar(self, area_id: str) -> str | None:
        routes = self._bundled.routes.get(area_id)
        return None if routes is None else routes.cet_market_tz

    def wants_again(self, area_id: str, document: PriceDocument) -> bool:
        """A day built without its rate is asked for once more when the rate has arrived since."""
        currency = self._currency(area_id)
        held = None if currency is None else document.fx_rate(currency)
        return self._rate.arrived(currency, document.day, held)

    def _currency(self, area_id: str) -> str | None:
        entry = self._bundled.catalogue.area(area_id)
        return None if entry is None else entry.currency

    def _area(self, area_id: str) -> EntsoeArea | None:
        routes = self._bundled.routes.get(area_id)
        if routes is None or routes.entsoe is None:
            return None
        return EntsoeArea.from_routes(area_id, routes)

    # -------------------------------------------------------------- fetching

    async def async_fetch_day(self, area_id: str, market_day: date) -> DayFetch:
        """One CET market day for an area; every expected failure is a code, never an exception."""
        area = self._area(area_id)
        entry = self._bundled.catalogue.area(area_id)
        token = self._keys.entsoe_key() if self._keys.entsoe_key_usable else None
        if area is None or entry is None or token is None or token.empty:
            return DayFetch("failed", code="source_missing")
        now = self._now()
        if self._quiet_until is not None and now < self._quiet_until:
            # Still inside the wait the platform asked for: no request.
            return DayFetch("failed", code=self._quiet_code or "rate_limited", retry_after=self._quiet_until - now)
        answer = await self._ask(token, area, market_day)
        if isinstance(answer, str):
            _LOGGER.debug("ENTSO-E %s %s: %s", area_id, market_day, answer)
            return DayFetch("failed", code=answer)
        if answer.retry_after is not None and answer.outcome == "failed":
            self._quiet_until = self._now() + answer.retry_after
            self._quiet_code = answer.code
        if answer.code == "entsoe_auth":
            _LOGGER.warning("ENTSO-E rejected the security key; its areas use another source until it is replaced")
            await self._keys.async_mark_entsoe_key(token, _REJECTED)
            return DayFetch("failed", code="entsoe_auth")
        if answer.outcome != "failed" and self._keys.entsoe_key_state == "unchecked":
            # Any answer past the credentials check proves the key.
            await self._keys.async_mark_entsoe_key(token, _ACCEPTED)
        if answer.outcome == "not_published":
            return DayFetch("not_published", code="not_published")
        if answer.outcome == "failed" or answer.day is None:
            _LOGGER.debug("ENTSO-E %s %s: %s", area_id, market_day, answer.code)
            return DayFetch("failed", code=answer.code or "entsoe_rejected", retry_after=answer.retry_after)
        fields = day_document_fields(answer.day)
        fx, fx_date = await self._rate.async_rate(entry.currency, market_day)
        document = build_day_document(
            **fields,
            tz=entry.tz,
            fx=fx,
            fx_date=fx_date,
            fx_src=FX_SRC if fx else None,
            retrieved=self._now(),
        )
        return DayFetch("fetched", document=document)

    async def async_check_key(self, token: EntsoeToken, area_id: str | None = None) -> KeyCheck:
        """Ask the platform once, with [token], for today's CET day of [area_id] (or of [CHECK_AREA] when it has
        no route): `ok` when it answered past the credentials check, `rejected` on 401/403, else `unreachable`.
        Nothing is cached or stored; the provider's own wait after a rate limit does not apply."""
        area = (self._area(area_id) if area_id is not None else None) or self._area(CHECK_AREA)
        if area is None or token.empty:
            return "unreachable"
        day = market_today(self._now(), area.market_tz)
        answer = await self._ask(token, area, day)
        if isinstance(answer, str):
            return "unreachable"
        if answer.code == "entsoe_auth":
            return "rejected"
        # A day, "no data" or a body the parser refuses: the platform looked past the credentials.
        if answer.outcome != "failed" or answer.code == "provider_refused":
            return "ok"
        return "unreachable"

    def _client(self) -> ClientSession:
        if self._session is None:
            self._session = async_get_clientsession(self._hass)
        return self._session

    async def _ask(self, token: EntsoeToken, area: EntsoeArea, day: date):
        """One request and what its answer means, or a transport code (`timeout`, `network`, `too_large`)."""
        try:
            request = (
                build_request(token, area, day)
                if self._endpoint is None
                else build_request(token, area, day, endpoint=self._endpoint)
            )
        except EntsoeRequestError:
            return "source_missing"
        async with self._lock:
            await self._space()
            try:
                status, body, retry_after = await self._send(request)
            finally:
                self._last_request = asyncio.get_running_loop().time()
        if isinstance(status, str):
            return status
        return read_response(status, body, area, day, now=self._now(), retry_after=retry_after, token=token)

    async def _space(self) -> None:
        if self._last_request is None or self._min_spacing_s <= 0:
            return
        wait = self._last_request + self._min_spacing_s - asyncio.get_running_loop().time()
        if wait > 0:
            await asyncio.sleep(wait)

    async def _send(self, request: EntsoeRequest) -> tuple[int | str, bytes, str | None]:
        """`(status, body, Retry-After)`, or `(code, b"", None)` for a request that brought no answer.

        The original error is not kept: only its kind leaves the `except` block, so nothing raised or returned
        from here carries its text (which may hold the request URL) as a message, cause or context.
        """
        headers = {"User-Agent": USER_AGENT, "Accept": "application/xml"}
        code: str
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                async with self._client().get(request.wire_url(), headers=headers) as response:
                    status = response.status
                    headers_in = getattr(response, "headers", None)
                    retry_after = None if headers_in is None else headers_in.get("Retry-After")
                    body = await _read_bounded(response.content)
            return status, body, retry_after
        except _TooLarge:
            code = "too_large"
        except TimeoutError:
            code = "timeout"
        except Exception:  # noqa: BLE001 - any failure of the request is a network failure, and its text is dropped
            code = "network"
        return code, b"", None
