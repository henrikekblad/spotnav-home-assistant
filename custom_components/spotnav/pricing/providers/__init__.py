"""Where a market-day file comes from: the price providers behind the repository.

The repository (`pricing/price_repository.py`) stays the one owner of cache, store, coalescing, snapshots and
states; a provider only answers "this area's file for this market day" as the relay's `"v": 1` day document,
a plain mapping the repository validates with the same `parse_day` as any other. So every source is checked by
one parser, the store keeps one format, and nothing that reads a [PriceDocument] knows where it came from.

Only the relay exists so far ([RelayProvider], `providers/relay.py`), and [ProviderRouter] answers it for
every area. The router is the seam where a choice per area will go (Nord Pool through Home Assistant, the
user's own ENTSO-E key); until then nothing about which files are asked for, or when, has changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol, TYPE_CHECKING

from ..relay_contract import ParseCode

if TYPE_CHECKING:
    from .relay import RelayProvider

#: Which provider a file came from, as stored beside it (`"provider"` in the store's day entries).
ProviderKey = Literal["relay"]

#: How a provider's attempt for one file ended. The relay has no `not_published` of its own: its index says
#: which days exist, so a file it does not have is a failed request (its 404), as it always was.
FetchOutcome = Literal["fetched", "not_published", "failed"]

#: A failed fetch's stable code: the transport half here, the content half `relay_contract`'s.
FetchCode = Literal["timeout", "network", "http_status", "too_large"]
FailureCode = FetchCode | ParseCode

#: The codes that mean "could not be reached", not "answered something wrong" (`stale`/`unavailable` vs
#: `invalid`).
TRANSPORT_CODES: frozenset[str] = frozenset({"timeout", "network", "http_status", "too_large"})


@dataclass(frozen=True, slots=True)
class DayFetch:
    """One provider's answer for one market-day file.

    `document` is set for `fetched` only: the raw day document, not yet validated. `code` is set for
    `failed` (and may be for `not_published`); it is never a response body or a URL.
    """

    outcome: FetchOutcome
    document: Any = None
    code: FailureCode | None = None


class PriceProvider(Protocol):
    """What the repository needs from a source of market-day files."""

    #: Stored beside each file it delivers.
    key: ProviderKey
    #: Whether it publishes an index of the days it holds (the relay); a provider without one is asked on a
    #: schedule instead.
    has_index: bool

    async def async_fetch_day(self, area_id: str, market_day: date) -> DayFetch:
        """The area's file for one market day, as published; never raises for an expected failure."""


class ProviderRouter:
    """Which provider serves an area. One provider so far: the relay, for every area."""

    def __init__(self, relay: RelayProvider) -> None:
        self._relay = relay

    @property
    def relay(self) -> RelayProvider:
        """The relay itself, for the resources only it has (its area list and index)."""
        return self._relay

    def provider_for(self, area_id: str) -> PriceProvider:
        """The provider whose files an area is read from."""
        return self._relay
