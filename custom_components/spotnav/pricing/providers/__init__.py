"""Where a market-day file comes from: the price providers behind the repository.

The repository (`pricing/price_repository.py`) stays the one owner of cache, store, coalescing, snapshots and
states; a provider only answers "this area's file for this market day" as a `"v": 1` day document (the shape
`pricing/relay_contract.py` reads), a plain mapping the repository validates with the same `parse_day` as any
other. So every source is checked by one parser, the store keeps one format, and nothing that reads a
[PriceDocument] knows where it came from.

Four providers, each fetched by this installation for its own use. None has an index: the publication schedule says
when a day is worth asking for ([schedule_authority]).

* Nord Pool through Home Assistant's own `nordpool` integration ([NordPoolProvider], `providers/nordpool.py`):
  its files are CET delivery days (`routes.cet_market_tz`);
* ENTSO-E's Transparency Platform with the installation's own key ([EntsoeProvider], `providers/entsoe.py`):
  asked like Nord Pool, on the same schedule and the same CET calendar;
* Spain's PVPC through Home Assistant's own `pvpc_hourly_pricing` integration ([PvpcProvider],
  `providers/pvpc.py`): the prices it already holds, on the Madrid day;
* Great Britain's Agile rates through the Octopus Energy integration ([OctopusProvider], `providers/octopus.py`):
  the customer's own meter's rates, on the Paris day (23:00 to 23:00 UK time).

[ProviderRouter] picks one per area ([choose_provider]): the PVPC integration for the PVPC area while one of its
entries is loaded; the Octopus integration for a GB area while an Agile meter's rates are there for it; Nord Pool
for an area Nord Pool trades while a `nordpool` config entry is loaded; else ENTSO-E for an area the platform
publishes while a key is set that it has not rejected; else **none**: the area has no source, nothing is asked for,
and its notice names the action that gives it one. There is no fallback from one to the other within an attempt.
The choice is taken again whenever a `nordpool`, `pvpc_hourly_pricing` or `octopus_energy` entry is added, removed,
loaded or unloaded (Home Assistant's `SIGNAL_CONFIG_ENTRY_CHANGED`, which it sends for each of those, a load or
unload being a state change of the entry), whenever one of the Octopus integration's rate entities changes (the
meter's tariff is read from it), and whenever the ENTSO-E key is set, cleared or rejected.

[ProviderRouter.source_status] says which provider an area is read from and, when it has none, the one action that
gives it one: the notices of `providers/notices.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final, Literal, Protocol, TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState, SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from ... import time_zones
from ..relay_contract import AreaPublication, ParseCode
from .notices import entry_state, EntryState, source_notice, SourceFacts, SourceStatus

if TYPE_CHECKING:
    from ..relay_contract import PriceDocument
    from .entsoe import EntsoeProvider
    from .nordpool import NordPoolProvider
    from .octopus import OctopusProvider
    from .pvpc import PvpcProvider

_LOGGER = logging.getLogger(__name__)

#: Which provider a file came from, as stored beside it (`"provider"` in the store's day entries). Files kept from
#: before 2.0, when the SpotNav relay delivered prices, carry `"relay"`; no provider has that key any more.
ProviderKey = Literal["nordpool", "entsoe", "pvpc", "octopus"]

#: How a provider's attempt for one file ended. An empty answer (Nord Pool's empty list, ENTSO-E's "no matching
#: data") is `not_published`: the day is not out yet, which is not a failure.
FetchOutcome = Literal["fetched", "not_published", "failed"]

#: A failed fetch's stable code: the transport half here, the content half `relay_contract`'s.
#:
#: * `nordpool_unavailable`: no loaded `nordpool` entry, or its service is missing;
#: * `nordpool_error`: Nord Pool could not be reached (its `connection_error`, or no answer in time);
#: * `nordpool_rejected`: Nord Pool refused the request (its `authentication_error`, or an area code the
#:   service does not accept);
#: * `provider_refused`: the answer is not a day this client can read (another area, a gap, a mixed step, a
#:   price that is not a number); never the answer's text;
#: * `entsoe_auth`: ENTSO-E rejected the key (401/403, or an acknowledgement naming a credential problem);
#: * `rate_limited`: ENTSO-E asked to wait (429, or a rate-limit acknowledgement); [DayFetch.retry_after] says how
#:   long, and the provider asks nothing until then;
#: * `entsoe_unavailable`: ENTSO-E answered with a server error;
#: * `entsoe_rejected`: ENTSO-E refused the request otherwise (another status, a bad-request acknowledgement);
#: * `pvpc_unavailable`: no loaded `pvpc_hourly_pricing` entry, or it holds no prices for a day that is out;
#: * `octopus_unavailable`: no Agile meter's rates for the area, or they do not reach back to the day's start;
#: * `fx_missing`: no ECB rate to convert a day priced in another currency (Octopus, in GBP) to EUR;
#: * `source_missing`: no way to ask this provider for the area (no key, no route);
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
    "entsoe_auth",
    "rate_limited",
    "entsoe_unavailable",
    "entsoe_rejected",
    "pvpc_unavailable",
    "octopus_unavailable",
    "fx_missing",
    "source_missing",
    "provider_refused",
    "not_published",
    "history_out_of_reach",
]
FailureCode = FetchCode | ParseCode

#: The codes that mean "could not be reached", not "answered something wrong" (`stale`/`unavailable` vs
#: `invalid`), and that back off.
TRANSPORT_CODES: frozenset[str] = frozenset(
    {
        "timeout",
        "network",
        "http_status",
        "too_large",
        "nordpool_unavailable",
        "nordpool_error",
        "nordpool_rejected",
        "entsoe_auth",
        "rate_limited",
        "entsoe_unavailable",
        "entsoe_rejected",
        "pvpc_unavailable",
        "octopus_unavailable",
        "fx_missing",
        "source_missing",
    }
)

#: How long before a provider's expected publication its window opens (the refresh manager's
#: `PUBLICATION_WINDOW_LEAD`): before it, tomorrow is not asked for.
PUBLICATION_WINDOW_LEAD: Final = timedelta(minutes=5)

#: What the schedule and the provider's last answer say about one file.
Authority = Literal["listed", "not_listed", "unknown"]


@dataclass(frozen=True, slots=True)
class DayFetch:
    """One provider's answer for one market-day file.

    `document` is set for `fetched` only: the raw day document, not yet validated. `code` is set for
    `failed` (and may be for `not_published`); it is never a response body or a URL. `retry_after` is the
    provider's own word on how long to wait (ENTSO-E's `Retry-After`); the provider itself keeps to it.
    """

    outcome: FetchOutcome
    document: Any = None
    code: FailureCode | None = None
    retry_after: timedelta | None = None


class PriceProvider(Protocol):
    """What the repository needs from a source of market-day files."""

    #: Stored beside each file it delivers.
    key: ProviderKey
    #: What the card and the app call it, and where it is (attribution).
    name: str
    url: str
    #: When tomorrow's file is expected: the schedule the provider is asked on ([schedule_authority]).
    publication: AreaPublication
    #: How far back a day can be asked for; `None` means no limit.
    history_reach: timedelta | None

    def market_calendar(self, area_id: str) -> str | None:
        """The zone whose calendar day one of its files covers; `None` for an area it has no route for."""

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


def choose_provider(
    *,
    nordpool_serves: bool,
    nordpool_loaded: bool,
    entsoe_serves: bool,
    entsoe_key_usable: bool,
    pvpc_serves: bool = False,
    pvpc_loaded: bool = False,
    octopus_serves: bool = False,
) -> ProviderKey | None:
    """The rule, per area: the PVPC integration for the PVPC area while one of its entries is loaded; the Octopus
    integration for a GB area while an Agile meter's rates are there for it; Nord Pool for an area it trades while
    a `nordpool` entry is loaded; else ENTSO-E for an area the platform publishes while a key is set that it has
    not rejected; else none (the area has no source). No area is served by more than one of the first three, so
    their order is only for reading."""
    if pvpc_serves and pvpc_loaded:
        return "pvpc"
    if octopus_serves:
        return "octopus"
    if nordpool_serves and nordpool_loaded:
        return "nordpool"
    if entsoe_serves and entsoe_key_usable:
        return "entsoe"
    return None


class ProviderRouter:
    """Which provider serves an area ([choose_provider]). Listeners hear when that choice may have changed for some
    area: a `nordpool` or `pvpc_hourly_pricing` entry loaded or unloaded, an Agile meter found or gone, the
    ENTSO-E key set, cleared, accepted or rejected.
    """

    def __init__(
        self,
        nordpool: NordPoolProvider | None = None,
        entsoe: EntsoeProvider | None = None,
        *,
        pvpc: PvpcProvider | None = None,
        octopus: OctopusProvider | None = None,
    ) -> None:
        self._nordpool = nordpool
        self._entsoe = entsoe
        self._pvpc = pvpc
        self._octopus = octopus
        self._nordpool_loaded = False
        self._pvpc_loaded = False
        # The integrations' entries as last seen ([entry_state]), for the notices; `None` until started.
        self._hass: HomeAssistant | None = None
        self._entry_states: dict[str, EntryState] = {}
        self._listeners: list[Callable[[], None]] = []
        self._unsubscribe: list[Callable[[], None]] = []

    @property
    def entsoe(self) -> EntsoeProvider | None:
        """The ENTSO-E provider, for checking a key before it is stored."""
        return self._entsoe

    @property
    def octopus(self) -> OctopusProvider | None:
        """The Octopus provider, for what it knows of the customer's meter (its region)."""
        return self._octopus

    @property
    def nordpool_in_use(self) -> bool:
        return self._nordpool is not None and self._nordpool_loaded

    def provider_for(self, area_id: str) -> PriceProvider | None:
        """The provider whose files an area is read from, or `None` when the area has no source."""
        key = choose_provider(
            nordpool_serves=self._nordpool is not None and self._nordpool.serves(area_id),
            nordpool_loaded=self._nordpool_loaded,
            entsoe_serves=self._entsoe is not None and self._entsoe.serves(area_id),
            entsoe_key_usable=self._entsoe is not None and self._entsoe.key_usable(),
            pvpc_serves=self._pvpc is not None and self._pvpc.serves(area_id),
            pvpc_loaded=self._pvpc_loaded,
            octopus_serves=self._octopus is not None and self._octopus.serves(area_id),
        )
        if key == "pvpc" and self._pvpc is not None:
            return self._pvpc
        if key == "octopus" and self._octopus is not None:
            return self._octopus
        if key == "nordpool" and self._nordpool is not None:
            return self._nordpool
        if key == "entsoe" and self._entsoe is not None:
            return self._entsoe
        return None

    def source_status(self, area_id: str) -> SourceStatus:
        """The area's provider (`None` fields when it has none) and its notice (`notices.py`)."""
        provider = self.provider_for(area_id)
        if provider is None:
            return SourceStatus(key=None, name=None, url=None, notice=source_notice(self.facts(area_id)))
        return SourceStatus(
            key=provider.key, name=provider.name, url=provider.url, notice=source_notice(self.facts(area_id))
        )

    def facts(self, area_id: str) -> SourceFacts:
        """What decides the area's source and notice now."""
        octopus = self._octopus
        octopus_meter = octopus is not None and octopus.serves(area_id)
        key_state = None if self._entsoe is None else self._entsoe.key_state()
        return SourceFacts(
            nordpool_route=self._nordpool is not None and self._nordpool.serves(area_id),
            nordpool_entry=self._entry_state("nordpool"),
            entsoe_route=self._entsoe is not None and self._entsoe.serves(area_id),
            entsoe_key="none" if key_state is None else key_state,
            pvpc_route=self._pvpc is not None and self._pvpc.serves(area_id),
            pvpc_entry=self._entry_state(PVPC_DOMAIN),
            octopus_route=octopus is not None and octopus.has_route(area_id),
            octopus_entry=self._entry_state(OCTOPUS_DOMAIN),
            octopus_meter=octopus_meter,
            octopus_region=octopus.region_mismatch(area_id) if octopus_meter and octopus is not None else None,
        )

    def _entry_state(self, domain: str) -> EntryState:
        return entry_state(self._hass, domain)

    @callback
    def async_start(self, hass: HomeAssistant) -> Callable[[], None]:
        """Follow the `nordpool`, `pvpc_hourly_pricing` and `octopus_energy` config entries, the Octopus rate
        entities and the ENTSO-E key from now on; returns the call that stops following."""
        self._hass = hass
        self._entry_states = {domain: entry_state(hass, domain) for domain in FOLLOWED_DOMAINS}
        self._nordpool_loaded = _nordpool_loaded(hass)
        self._pvpc_loaded = self._pvpc is not None and self._pvpc.loaded()
        if self._octopus is not None:
            self._octopus.refresh()

        @callback
        def changed(_change: Any, entry: Any) -> None:
            domain = getattr(entry, "domain", None)
            if domain not in FOLLOWED_DOMAINS:
                return
            # An entry added, failed or removed changes an area's notice, if not its provider.
            state = entry_state(hass, domain)
            moved = state != self._entry_states.get(domain)
            self._entry_states[domain] = state
            if domain == "nordpool":
                loaded = _nordpool_loaded(hass)
                if loaded != self._nordpool_loaded:
                    self._nordpool_loaded = loaded
                    _LOGGER.debug("Nord Pool through Home Assistant is %s", "in use" if loaded else "not in use")
                    moved = True
            elif domain == PVPC_DOMAIN and self._pvpc is not None:
                loaded = self._pvpc.loaded()
                if loaded != self._pvpc_loaded:
                    self._pvpc_loaded = loaded
                    _LOGGER.debug("The PVPC through Home Assistant is %s", "in use" if loaded else "not in use")
                    moved = True
            elif domain == OCTOPUS_DOMAIN and self._octopus is not None and self._octopus.refresh():
                _LOGGER.debug("Agile meters through Octopus Energy: %d", len(self._octopus.meters))
                moved = True
            if moved:
                self._notify()

        @callback
        def octopus_changed() -> None:
            if self._octopus is not None and self._octopus.refresh():
                _LOGGER.debug("Agile meters through Octopus Energy: %d", len(self._octopus.meters))
                self._notify()

        @callback
        def rate_entity_changed(_event: Any) -> None:
            octopus_changed()

        @callback
        def rate_entity_filter(event_data: Any) -> bool:
            entity_id = event_data.get("entity_id")
            return isinstance(entity_id, str) and self._octopus is not None and self._octopus.is_rate_entity(entity_id)

        self._unsubscribe.append(async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, changed))
        if self._octopus is not None:
            # The meter's tariff, and so its region and whether it is Agile, is read from its rate entities.
            self._unsubscribe.append(
                hass.bus.async_listen(EVENT_STATE_CHANGED, rate_entity_changed, event_filter=rate_entity_filter)
            )
        if self._entsoe is not None:
            self._unsubscribe.append(self._entsoe.add_key_listener(self._notify))
        return self.async_stop

    @callback
    def async_stop(self) -> None:
        while self._unsubscribe:
            self._unsubscribe.pop()()

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception as err:  # noqa: BLE001 - one listener must not stop the rest
                _LOGGER.warning("A price provider listener failed: %s", type(err).__name__)

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call [listener] (on the loop, no arguments) whenever the choice of provider may have changed."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove


#: The integrations whose entries the router follows besides `nordpool` (their providers' `DOMAIN`, repeated here
#: so this module does not import the providers it is imported by).
PVPC_DOMAIN: Final = "pvpc_hourly_pricing"
OCTOPUS_DOMAIN: Final = "octopus_energy"
#: Every integration whose entries decide a provider or a notice.
FOLLOWED_DOMAINS: Final = ("nordpool", PVPC_DOMAIN, OCTOPUS_DOMAIN)


def _nordpool_loaded(hass: HomeAssistant) -> bool:
    return any(entry.state is ConfigEntryState.LOADED for entry in hass.config_entries.async_entries("nordpool"))
