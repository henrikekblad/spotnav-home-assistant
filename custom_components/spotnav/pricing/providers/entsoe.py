"""Day-ahead prices from ENTSO-E's Transparency Platform, with the installation's own security token.

The request, the answer and the document are `pricing/entsoe_parse.py`'s (a port of the relay's client); this
module is the transport around it:

* **One request per file** (or per range of past files, for the history backfill), through Home Assistant's shared aiohttp session, with SpotNav's User-Agent, one overall
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
* **History reaches years back** ([history_reach] `None`). The history backfill (`pricing/price_history.py`) asks
  for up to [RANGE_DAYS] consecutive days in one request ([async_fetch_days]); each day is read from that answer
  exactly as if it had been asked alone.
* **The rate**: the area's ECB rate, as for Nord Pool (`providers/rates.py`). `src` is `"entsoe.eu"`.

The token is read from the key source on every request and lives only inside an `EntsoeRequest`. Nothing here logs
a URL, a body or an error's text: a transport failure is reduced to its code while the original error is dropped
inside the `except` block (its text may hold the request URL), so no exception, cause or context that leaves this
module can reach the token.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from typing import Final, Literal, overload, Protocol

from aiohttp import ClientSession
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from ..area_catalogue import BundledCatalogue
from ..day_document import build_day_document
from ..entsoe_parse import (
    build_request,
    day_document_fields,
    EntsoeAnswer,
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

#: The most consecutive days one request asks for (the history backfill): a month of quarter hours is a few hundred
#: kilobytes, well inside the body limit.
RANGE_DAYS: Final = 31

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
    #: What the card and the app call it, and where it is (attribution).
    name: str = "ENTSO-E Transparency Platform"
    url: str = "https://transparency.entsoe.eu/"
    has_index = False
    publication: AreaPublication = PUBLICATION
    #: The platform keeps years of day-ahead prices.
    history_reach: timedelta | None = None
    #: How many consecutive past days one request may ask for ([async_fetch_days]).
    history_range_days: int = RANGE_DAYS

    def __init__(
        self,
        hass: HomeAssistant,
        bundled: BundledCatalogue,
        keys: KeySource,
        fx: RateSource | None,
        *,
        session: ClientSession | None = None,
        now: Callable[[], datetime] | None = None,
        min_spacing_s: float | None = None,
        endpoint: str | None = None,
    ) -> None:
        self._hass = hass
        self._bundled = bundled
        self._keys = keys
        self._session = session
        self._now = now if now is not None else dt_util.utcnow
        self._rate = DayRate(fx, self._now)
        self._min_spacing_s = MIN_SPACING_S if min_spacing_s is None else min_spacing_s
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

    def key_state(self) -> str | None:
        """The stored key's state (`ok`, `unchecked`, `rejected`), `None` with no key."""
        return self._keys.entsoe_key_state

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
        ready = self._ready(area_id)
        if isinstance(ready, DayFetch):
            return ready
        area, token = ready
        answer = await self._ask(token, area, market_day)
        return await self._settle(area_id, market_day, token, answer)

    async def async_fetch_days(self, area_id: str, days: Sequence[date]) -> dict[date, DayFetch]:
        """Consecutive CET market days in **one** request (the history backfill), at most [RANGE_DAYS]: the platform
        answers a period of several days, and each day is read from that one answer as if asked alone. A day the
        answer does not carry is `not_published`; a failure of the request is every day's."""
        ordered = sorted(set(days))
        if not ordered:
            return {}
        if len(ordered) > RANGE_DAYS or (ordered[-1] - ordered[0]).days != len(ordered) - 1:
            raise ValueError("a range is at most RANGE_DAYS consecutive days")
        if len(ordered) == 1:
            return {ordered[0]: await self.async_fetch_day(area_id, ordered[0])}
        ready = self._ready(area_id)
        if isinstance(ready, DayFetch):
            return dict.fromkeys(ordered, ready)
        area, token = ready
        answers = await self._ask(token, area, ordered[0], ordered)
        first = answers if isinstance(answers, str) else answers[ordered[0]]
        if isinstance(first, str) or (first.outcome == "failed" and first.code != "provider_refused"):
            # The request itself failed (transport, status, acknowledgement): one answer for every day, settled
            # once so a rejected key or a wait is recorded once.
            return dict.fromkeys(ordered, await self._settle(area_id, ordered[0], token, first))
        assert not isinstance(answers, str)
        return {day: await self._settle(area_id, day, token, answers[day]) for day in ordered}

    def _ready(self, area_id: str) -> tuple[EntsoeArea, EntsoeToken] | DayFetch:
        """The area and the key to ask with, or the answer that needs no request (no route or key, or still
        inside the wait the platform asked for)."""
        area = self._area(area_id)
        entry = self._bundled.catalogue.area(area_id)
        token = self._keys.entsoe_key() if self._keys.entsoe_key_usable else None
        if area is None or entry is None or token is None or token.empty:
            return DayFetch("failed", code="source_missing")
        now = self._now()
        if self._quiet_until is not None and now < self._quiet_until:
            # Still inside the wait the platform asked for: no request.
            return DayFetch("failed", code=self._quiet_code or "rate_limited", retry_after=self._quiet_until - now)
        return area, token

    async def _settle(self, area_id: str, market_day: date, token: EntsoeToken, answer: EntsoeAnswer | str) -> DayFetch:
        """What one answer means for the provider (a wait, a rejected or proven key) and the day it brings."""
        entry = self._bundled.catalogue.area(area_id)
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
        if answer.outcome == "failed" or answer.day is None or entry is None:
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

    @overload
    async def _ask(self, token: EntsoeToken, area: EntsoeArea, day: date) -> EntsoeAnswer | str: ...

    @overload
    async def _ask(
        self, token: EntsoeToken, area: EntsoeArea, day: date, days: Sequence[date]
    ) -> dict[date, EntsoeAnswer] | str: ...

    async def _ask(self, token: EntsoeToken, area: EntsoeArea, day: date, days: Sequence[date] | None = None):
        """One request and what its answer means, or a transport code (`timeout`, `network`, `too_large`).

        With [days] (consecutive, from [day]) the request covers them all and the answer is read once per day, off
        the event loop: a month of quarter hours is a large document to read thirty times."""
        last = None if days is None else days[-1]
        try:
            request = (
                build_request(token, area, day, last=last)
                if self._endpoint is None
                else build_request(token, area, day, endpoint=self._endpoint, last=last)
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
        now = self._now()
        if days is None:
            return read_response(status, body, area, day, now=now, retry_after=retry_after, token=token)

        def read_each() -> dict[date, EntsoeAnswer]:
            return {
                each: read_response(status, body, area, each, now=now, retry_after=retry_after, token=token)
                for each in days
            }

        return await self._hass.async_add_executor_job(read_each)

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
