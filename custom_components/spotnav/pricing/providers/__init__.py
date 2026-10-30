"""Where a market-day file comes from: the price providers behind the repository.

The repository (`pricing/price_repository.py`) stays the one owner of cache, store, coalescing, snapshots and
states; a provider only answers "this area's file for this market day" as the relay's `"v": 1` day document,
a plain mapping the repository validates with the same `parse_day` as any other. So every source is checked by
one parser, the store keeps one format, and nothing that reads a [PriceDocument] knows where it came from.

Two providers so far:

* the relay ([RelayProvider], `providers/relay.py`): its index says which days exist, and its files are dated on
  the area list's own market calendar;
* Nord Pool through Home Assistant's own `nordpool` integration ([NordPoolProvider], `providers/nordpool.py`):
  no index, so the publication schedule says when a day is worth asking for ([schedule_authority]), and its
  files are CET delivery days (`routes.cet_market_tz`).

[ProviderRouter] picks one per area: Nord Pool for an area Nord Pool trades while a `nordpool` config entry is
loaded, else the relay. There is no fallback from one to the other within an attempt. The choice is taken again
whenever a `nordpool` entry is added, removed, loaded or unloaded (Home Assistant's `SIGNAL_CONFIG_ENTRY_CHANGED`,
which it sends for each of those, a load or unload being a state change of the entry).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final, Literal, Protocol, TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState, SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from ... import time_zones
from ..relay_contract import AreaPublication, ParseCode

if TYPE_CHECKING:
    from ..relay_contract import PriceDocument
    from .nordpool import NordPoolProvider
    from .relay import RelayProvider

_LOGGER = logging.getLogger(__name__)

#: Which provider a file came from, as stored beside it (`"provider"` in the store's day entries).
ProviderKey = Literal["relay", "nordpool"]

#: How a provider's attempt for one file ended. The relay has no `not_published` of its own: its index says
#: which days exist, so a file it does not have is a failed request (its 404), as it always was. Nord Pool's
#: empty answer is `not_published`: the day is not out yet, which is not a failure.
FetchOutcome = Literal["fetched", "not_published", "failed"]

#: A failed fetch's stable code: the transport half here, the content half `relay_contract`'s.
#:
#: * `nordpool_unavailable`: no loaded `nordpool` entry, or its service is missing;
#: * `nordpool_error`: Nord Pool could not be reached (its `connection_error`, or no answer in time);
#: * `nordpool_rejected`: Nord Pool refused the request (its `authentication_error`, or an area code the
#:   service does not accept);
#: * `provider_refused`: the answer is not a day this client can read (another area, a gap, a mixed step, a
#:   price that is not a number); never the answer's text;
#: * `not_published`: carried by a `not_published` outcome, never recorded as a failure;
#: * `history_out_of_reach`: an archive day older than the provider keeps, not asked for.
FetchCode = Literal[
    "timeout",
    "network",
    "http_status",
    "too_large",
    "nordpool_unavailable",
    "nordpool_error",
    "nordpool_rejected",
    "provider_refused",
    "not_published",
    "history_out_of_reach",
]
FailureCode = FetchCode | ParseCode

#: The codes that mean "could not be reached", not "answered something wrong" (`stale`/`unavailable` vs
#: `invalid`), and that back off.
TRANSPORT_CODES: frozenset[str] = frozenset(
    {"timeout", "network", "http_status", "too_large", "nordpool_unavailable", "nordpool_error", "nordpool_rejected"}
)

#: How long before a provider's expected publication its window opens (the refresh manager's
#: `PUBLICATION_WINDOW_LEAD`): before it, tomorrow is not asked for.
PUBLICATION_WINDOW_LEAD: Final = timedelta(minutes=5)

#: What a source says about one file: the relay's index, or the schedule for a provider without one.
Authority = Literal["listed", "not_listed", "unknown"]


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
    #: schedule instead ([schedule_authority]).
    has_index: bool
    #: When tomorrow's file is expected; `None` means the area list's own (the relay's).
    publication: AreaPublication | None
    #: How far back a day can be asked for; `None` means no limit.
    history_reach: timedelta | None

    def market_calendar(self, area_id: str) -> str | None:
        """The zone whose calendar day one of its files covers; `None` means the area list's `market_tz`."""

    def wants_again(self, area_id: str, document: PriceDocument) -> bool:
        """Whether a held file is worth asking for once more (a rate that was missing when it was built)."""

    async def async_fetch_day(self, area_id: str, market_day: date) -> DayFetch:
        """The area's file for one market day, as published; never raises for an expected failure."""


def publication_instant(market_day: date, publication: AreaPublication) -> datetime:
    """When a market day's file is expected: the day before, at the publication's own wall-clock time."""
    zone = time_zones.require_zone(publication.tz)
    return datetime.combine(market_day - timedelta(days=1), publication.local_time, tzinfo=zone)


def market_today(now: datetime, market_tz: str) -> date:
    return now.astimezone(time_zones.require_zone(market_tz)).date()


def schedule_open(day: date, *, now: datetime, market_tz: str, publication: AreaPublication) -> bool:
    """Whether a market day is worth a request from a provider without an index.

    Every day up to the market's today has been published; tomorrow from just before its expected publication;
    nothing later.
    """
    today = market_today(now, market_tz)
    if day <= today:
        return True
    if day > today + timedelta(days=1):
        return False
    return now >= publication_instant(day, publication) - PUBLICATION_WINDOW_LEAD


def schedule_authority(
    day: date,
    *,
    held: bool,
    now: datetime,
    market_tz: str,
    publication: AreaPublication,
    attempt_at: datetime | None,
    attempt_failed: bool,
    not_published_at: datetime | None,
) -> Authority:
    """Which files exist, for a provider without an index: the schedule plus the provider's last answer.

    * a held file is `listed`;
    * a day after tomorrow, or tomorrow before its window, is `not_listed` (no request is made for it);
    * the market's today or earlier, not held, is `unknown`: it has been published, so it is asked for;
    * tomorrow inside or after its window is `not_listed` while the last attempt answered "not published yet",
      `unknown` when the last attempt failed or none was made yet.
    """
    if held:
        return "listed"
    if not schedule_open(day, now=now, market_tz=market_tz, publication=publication):
        return "not_listed"
    if day <= market_today(now, market_tz) or attempt_failed:
        return "unknown"
    if not_published_at is not None and attempt_at is not None and not_published_at >= attempt_at:
        return "not_listed"
    return "unknown"


class ProviderRouter:
    """Which provider serves an area: Nord Pool for an area it trades while a `nordpool` entry is loaded, else
    the relay. Listeners hear when that choice may have changed for some area.
    """

    def __init__(self, relay: RelayProvider, nordpool: NordPoolProvider | None = None) -> None:
        self._relay = relay
        self._nordpool = nordpool
        self._nordpool_loaded = False
        self._listeners: list[Callable[[], None]] = []
        self._unsubscribe: Callable[[], None] | None = None

    @property
    def relay(self) -> RelayProvider:
        """The relay itself, for the resources only it has (its area list and index)."""
        return self._relay

    @property
    def nordpool_in_use(self) -> bool:
        return self._nordpool is not None and self._nordpool_loaded

    def provider_for(self, area_id: str) -> PriceProvider:
        """The provider whose files an area is read from."""
        if self._nordpool is not None and self._nordpool_loaded and self._nordpool.serves(area_id):
            return self._nordpool
        return self._relay

    @callback
    def async_start(self, hass: HomeAssistant) -> Callable[[], None]:
        """Follow the `nordpool` config entries from now on; returns the call that stops following."""
        self._nordpool_loaded = _nordpool_loaded(hass)

        @callback
        def changed(_change: Any, entry: Any) -> None:
            if getattr(entry, "domain", None) != "nordpool":
                return
            loaded = _nordpool_loaded(hass)
            if loaded == self._nordpool_loaded:
                return
            self._nordpool_loaded = loaded
            _LOGGER.debug("Nord Pool through Home Assistant is %s", "in use" if loaded else "not in use")
            for listener in list(self._listeners):
                try:
                    listener()
                except Exception as err:  # noqa: BLE001 - one listener must not stop the rest
                    _LOGGER.warning("A price provider listener failed: %s", type(err).__name__)

        self._unsubscribe = async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, changed)
        return self.async_stop

    @callback
    def async_stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call [listener] (on the loop, no arguments) whenever the choice of provider may have changed."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove


def _nordpool_loaded(hass: HomeAssistant) -> bool:
    return any(entry.state is ConfigEntryState.LOADED for entry in hass.config_entries.async_entries("nordpool"))
