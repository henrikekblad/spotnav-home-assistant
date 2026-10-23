"""One electricity price entry's live object: its area's prices with fees, and the day's cheap and dear hours.

Owned by the entry (`entry.runtime_data.device`) and stopped by its unload. It subscribes to its area
through the installation's one `PriceRefreshManager` under its own owner (`price:<entry id>`), so it
shares the area's one fetch with every charger and releases only its own share. It keeps exactly one
timer, at the next interval boundary, and tells its entities when the price, the hours or the
interval changes; the entities poll nothing. The arithmetic is `pricing/price_points.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any, Final

from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from . import time_zones
from .const import CONF_PRICE_AREA
from .planning.planner import FiscalChoice, PlannerInputError
from .pricing.market_day import display_day_span
from .pricing.price_points import (
    CHEAP,
    DEFAULT_HOURS,
    EXPENSIVE,
    fiscal_for,
    HOURS_STEP,
    MAX_HOURS,
    next_boundary,
    next_price_change,
    next_switch,
    overrides_from_data,
    point_at,
    price_points,
    PricePoint,
    rank,
    Ranking,
)
from .pricing.price_refresh import AreaPriceSnapshot, PriceRefreshManager
from .pricing.relay_contract import AreaEntry


_LOGGER = logging.getLogger(__name__)

#: The owner prefix a price entry subscribes under: not a charger's, so the two are separate owners of
#: one stream.
OWNER_PREFIX: Final = "price:"

#: Delay before trying again to subscribe after the relay or the catalogue was not there.
RETRY_DELAY_S: Final = 60.0

#: The longest span `spotnav.get_prices` answers for.
MAX_SPAN_DAYS: Final = 7


def snap_hours(value: float) -> float:
    """Hours per day as the numbers keep them: 0 to 24, in quarter-hours."""
    clamped = min(max(float(value), 0.0), MAX_HOURS)
    return round(clamped / HOURS_STEP) * HOURS_STEP


class PriceDevice:
    """The prices of one area with one set of fees, ranked per day, and the timer that moves them on."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        data: Mapping[str, Any],
        manager: PriceRefreshManager,
        *,
        now: Callable[[], datetime] | None = None,
        scheduler: Callable[..., Callable[[], None]] | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self.area_id: str = str(data.get(CONF_PRICE_AREA) or "")
        self.overrides = overrides_from_data(self.area_id, data)
        self._manager = manager
        self._now = now or dt_util.utcnow
        self._scheduler = scheduler or async_track_point_in_utc_time
        self.owner_id = f"{OWNER_PREFIX}{entry_id}"
        self._hours: dict[str, float] = dict(DEFAULT_HOURS)
        self._snapshot: AreaPriceSnapshot | None = None
        self._points: tuple[PricePoint, ...] = ()
        self._rankings: dict[str, Ranking] = {}
        self._problem: str | None = "loading"
        self._listeners: list[Callable[[], None]] = []
        self._unsubscribe: Callable[[], None] | None = None
        self._cancel_timer: Callable[[], None] | None = None
        self._cancel_retry: Callable[[], None] | None = None
        self._remove_catalogue: Callable[[], None] | None = None
        self._subscribing = False
        self._closed = False
        self._rank()

    # ----------------------------------------------------------------- lifetime

    async def async_start(self) -> None:
        """Subscribe to the area; a relay that is not there yet is retried, never an error."""
        await self._async_subscribe()
        if not self._closed:
            self._remove_catalogue = self._manager.add_catalogue_listener(self._on_catalogue)

    async def async_stop(self) -> None:
        """Release the area, the timers and the listeners. Terminal."""
        self._closed = True
        for cancel in (self._cancel_timer, self._cancel_retry, self._remove_catalogue, self._unsubscribe):
            if cancel is not None:
                cancel()
        self._cancel_timer = self._cancel_retry = self._remove_catalogue = self._unsubscribe = None
        self._listeners.clear()

    @property
    def subscribed(self) -> bool:
        return self._unsubscribe is not None

    async def _async_subscribe(self) -> None:
        if self._closed or self._unsubscribe is not None or self._subscribing or not self.area_id:
            return
        self._subscribing = True
        try:
            await self._manager.async_ensure_catalogue()
            handle = await self._manager.async_subscribe(
                owner_id=self.owner_id, area_id=self.area_id, listener=self._on_snapshot
            )
        except Exception as err:  # noqa: BLE001 - no prices yet is a state, not a failed setup
            _LOGGER.debug("Electricity price %s could not subscribe to %s: %s", self._entry_id, self.area_id, err)
            self._problem = "area_unavailable"
            self._schedule_retry()
            self._notify()
            return
        finally:
            self._subscribing = False
        if self._closed:
            handle()
            return
        self._unsubscribe = handle
        if self._cancel_retry is not None:
            self._cancel_retry()
            self._cancel_retry = None

    def _schedule_retry(self) -> None:
        if self._closed or self._cancel_retry is not None:
            return

        @callback
        def _retry(_now: Any) -> None:
            self._cancel_retry = None
            if not self._closed:
                self._hass.async_create_task(self._async_subscribe())

        self._cancel_retry = async_call_later(self._hass, RETRY_DELAY_S, _retry)

    @callback
    def _on_catalogue(self, snapshot: Any) -> None:
        """A new area list: try again now if this entry waits for its area, else price again (the area's
        suggestions may have moved)."""
        if self._closed or getattr(snapshot, "catalogue", None) is None:
            return
        if self._unsubscribe is None:
            self._hass.async_create_task(self._async_subscribe())
            return
        self._rebuild()
        self._notify()

    @callback
    def _on_snapshot(self, snapshot: AreaPriceSnapshot) -> None:
        if self._closed:
            return
        self._snapshot = snapshot
        self._rebuild()
        self._notify()

    # ----------------------------------------------------------------- the prices

    def _rebuild(self) -> None:
        snapshot = self._snapshot
        area = self.area
        fiscal = self.fiscal
        documents = []
        if snapshot is not None:
            documents = [doc for doc in (snapshot.today_document, snapshot.tomorrow_document) if doc is not None]
        if area is None:
            self._points, self._problem = (), "area_unavailable"
        elif fiscal is None:
            self._points, self._problem = (), "fees_unresolved"
        else:
            try:
                self._points = price_points(documents, currency=area.currency, fiscal=fiscal)
                self._problem = None if self._points else "no_prices"
            except PlannerInputError as err:
                self._points, self._problem = (), err.code
        self._rank()

    def _rank(self) -> None:
        self._rankings = {kind: rank(self._points, self._hours[kind], kind) for kind in (CHEAP, EXPENSIVE)}
        self._schedule_boundary()

    def _schedule_boundary(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        if self._closed:
            return
        boundary = next_boundary(self._points, self._now())
        if boundary is not None:
            self._cancel_timer = self._scheduler(self._hass, self._on_boundary, boundary)

    @callback
    def _on_boundary(self, _fired: datetime) -> None:
        self._cancel_timer = None
        if self._closed:
            return
        self._schedule_boundary()
        self._notify()

    # ----------------------------------------------------------------- listeners

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # ----------------------------------------------------------------- reads

    @property
    def area(self) -> AreaEntry | None:
        """The area's catalogue entry, or `None` while the catalogue does not list it."""
        return self._manager.catalogue_snapshot().area(self.area_id) if self.area_id else None

    @property
    def fiscal(self) -> FiscalChoice | None:
        return fiscal_for(self.overrides, self.area)

    @property
    def points(self) -> tuple[PricePoint, ...]:
        return self._points

    @property
    def problem(self) -> str | None:
        """Why there are no prices, as a stable code, or `None`."""
        return self._problem

    def hours(self, kind: str) -> float:
        return self._hours[kind]

    def set_hours(self, kind: str, value: float) -> None:
        """Hours per day for `cheap` or `expensive`: ranked again at once."""
        snapped = snap_hours(value)
        if self._hours.get(kind) == snapped:
            return
        self._hours[kind] = snapped
        self._rank()
        self._notify()

    def now(self) -> datetime:
        return self._now()

    def current(self, now: datetime | None = None) -> PricePoint | None:
        return point_at(self._points, now or self._now())

    def ranking(self, kind: str) -> Ranking:
        return self._rankings[kind]

    def is_on(self, kind: str, now: datetime | None = None) -> bool | None:
        """Whether `cheap` or `expensive` holds now; `None` when there is no price for now."""
        point = self.current(now)
        return None if point is None else point.start in self._rankings[kind].chosen

    def threshold(self, kind: str, now: datetime | None = None) -> float | None:
        point = self.current(now)
        return None if point is None else self._rankings[kind].thresholds.get(point.day)

    def next_switch(self, kind: str, now: datetime | None = None) -> datetime | None:
        return next_switch(self._points, self._rankings[kind], now or self._now())

    def next_price_change(self, now: datetime | None = None) -> datetime | None:
        return next_price_change(self._points, now or self._now())

    @property
    def tomorrow_published(self) -> bool:
        snapshot = self._snapshot
        return snapshot is not None and snapshot.tomorrow_document is not None

    def today(self) -> date | None:
        snapshot = self._snapshot
        return None if snapshot is None else snapshot.today

    def points_between(self, start: datetime, end: datetime, repository: Any) -> tuple[PricePoint, ...]:
        """The prices from [start] to [end] (UTC) from what the installation holds, today's and
        tomorrow's and any earlier day still kept. Nothing is fetched."""
        area, fiscal = self.area, self.fiscal
        if area is None or fiscal is None or end <= start:
            return ()
        zone = time_zones.require_zone(area.tz)
        first = start.astimezone(zone).date()
        last = (end - timedelta(microseconds=1)).astimezone(zone).date()
        if (last - first).days >= MAX_SPAN_DAYS:
            last = first + timedelta(days=MAX_SPAN_DAYS - 1)
        documents = []
        day = first
        while day <= last:
            document = repository.day_snapshot(self.area_id, day).document
            if document is not None:
                documents.append(document)
            day += timedelta(days=1)
        try:
            points = price_points(documents, currency=area.currency, fiscal=fiscal)
        except PlannerInputError:
            return ()
        return tuple(point for point in points if start <= point.start < end)

    def default_span(self) -> tuple[datetime, datetime] | None:
        """Today and, once published, tomorrow, as instants, or `None` without an area or a date."""
        area, today = self.area, self.today()
        if area is None or today is None:
            return None
        start, _ = display_day_span(today, area.tz)
        _, end = display_day_span(today + timedelta(days=1 if self.tomorrow_published else 0), area.tz)
        return start, end

    def diagnostics(self) -> dict[str, Any]:
        """What a support question needs: the area, the fees as resolved, the hours and the state. Never a
        price array."""
        now = self._now()
        fiscal = self.fiscal
        point = self.current(now)
        snapshot = self._snapshot
        return {
            "area_id": self.area_id,
            "owner_id": self.owner_id,
            "subscribed": self.subscribed,
            "problem": self._problem,
            "fees": {
                component: getattr(self.overrides, component).as_dict() for component in ("vat", "tax", "transfer")
            },
            "fiscal": None
            if fiscal is None
            else {
                "vat_percent": fiscal.vat_percent if fiscal.vat_enabled else None,
                "tax_minor_per_kwh": fiscal.tax_minor_per_kwh if fiscal.tax_enabled else None,
                "transfer_minor_per_kwh": fiscal.transfer_minor_per_kwh if fiscal.transfer_enabled else None,
            },
            "hours": dict(self._hours),
            "price_state": None if snapshot is None else snapshot.state,
            "today": None if snapshot is None else snapshot.today.isoformat(),
            "tomorrow_published": self.tomorrow_published,
            "intervals": len(self._points),
            "current": None
            if point is None
            else {"start": point.start.isoformat(), "end": point.end.isoformat(), "price": point.price},
            "cheap_now": self.is_on(CHEAP, now),
            "expensive_now": self.is_on(EXPENSIVE, now),
            "timer_armed": self._cancel_timer is not None,
        }
