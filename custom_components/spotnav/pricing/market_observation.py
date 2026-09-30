"""One charger's display need for its selected area's prices.

The graph belongs to the market the settings name, not to the planner: the strategy decides who owns
the plan, never whether a price curve exists (a solar charger has no calculation subscription and
still needs its graph). So every loaded charger entry owns one of these: a subscription to its
settings' area whenever the catalogue knows it, whatever the strategy. It has no product of its own;
the dashboard reads the documents through the shared repository. It shares `PriceRefreshManager`'s
per-area stream with the Auto subscription (a refcount, not a second downloader), so neither
consumer can drop the other's stream while it still needs it.

Lifetime:

* the subscription follows the area a settings change names (applied through the Auto controller,
  which reconciles this observation too);
* it is released when the entry unloads; a reload subscribes again from the restored settings;
* a generation makes a delivery from an area this observation has moved away from inert, so nothing
  is ever shown under the wrong market.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Final

from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.event import async_call_later

from ..planning.auto_settings import AutoSettings, AutoSettingsStore
from .price_refresh import CatalogueSnapshot, PriceRefreshManager


#: The owner id a display observation subscribes under; not the Auto controller's prefix, so a
#: charger's graph and calculation are two owners of one stream.
_LOGGER = logging.getLogger(__name__)

OBSERVATION_OWNER_PREFIX: Final = "observation:"

#: Delay before retrying after a transient failure: this observation's own appointment, cancelled
#: on success, independent of the manager's cadence.
RECOVERY_DELAY_SECONDS: Final = 60.0


def _default_recovery_scheduler(
    hass: HomeAssistant,
) -> Callable[[Callable[[], None]], Callable[[], None]]:

    def schedule(action: Callable[[], None]) -> Callable[[], None]:
        # Marked `@callback`: an unmarked function runs in a worker thread, where
        # `async_create_task` raises and the retry silently dies.
        @callback
        def fire(_now: Any) -> None:
            action()

        return async_call_later(hass, RECOVERY_DELAY_SECONDS, fire)

    return schedule



class MarketObservation:

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        store: AutoSettingsStore,
        manager: PriceRefreshManager,
        *,
        schedule_recovery: Callable[[Callable[[], None]], Callable[[], None]] | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store = store
        self._manager = manager
        self._owner_id = f"{OBSERVATION_OWNER_PREFIX}{entry_id}"
        # The published subscription and the manager owner holding it.
        self._unsubscribe: Callable[[], None] | None = None
        self._published_owner: str | None = None
        self._schedule_recovery: Callable[[Callable[[], None]], Callable[[], None]] = (
            schedule_recovery or _default_recovery_scheduler(hass)
        )
        self._recovery_cancel: Callable[[], None] | None = None
        self._area: str | None = None
        self._observed_area: str | None = None
        # The admission counter, the only ordering fact here: it rises once per admitted reconcile
        # and once on close. A reconcile is stale once it is no longer the newest admitted one.
        self._admission = 0
        self._closed = False
        # The manager's catalogue signal is the other half of recovery; it only wakes an
        # observation that is empty and waiting.
        self._remove_catalogue_listener: Callable[[], None] | None = manager.add_catalogue_listener(
            self._on_catalogue_changed
        )

    @property
    def owner_id(self) -> str:
        """The stable logical owner (one per charger entry), for diagnostics."""
        return self._owner_id

    @property
    def recovery_pending(self) -> bool:
        return self._recovery_cancel is not None

    @property
    def published_owner(self) -> str | None:
        return self._published_owner

    @property
    def subscribed_area(self) -> str | None:
        return self._area

    @property
    def observed_area(self) -> str | None:
        """The area of the last snapshot this observation accepted (the generation guard's bookkeeping)."""
        return self._observed_area

    @property
    def closed(self) -> bool:
        return self._closed

    async def async_start(self) -> None:
        await self.async_reconcile(self._store.settings(self._entry_id))

    async def async_reconcile(self, settings: AutoSettings) -> None:
        """Follow [settings]'s area: keep it fresh, or release what this entry had.

        Called from the one place a settings change is applied, so every path that can move a charger's area
        moves this subscription with it. An unknown area, and no area, both mean nothing to observe.

        Ordering: two settings changes can overlap while one waits for the catalogue. Every call is admitted
        before its first await and, after every await, re-checks in one synchronous step that it is still
        the newest admitted call and the observation still open; only then may it touch the subscription. An
        obsolete call releases any handle it created and changes nothing. The check is a comparison, not a
        lock: a lock would have to be held across the catalogue fetch, and on the event loop a synchronous
        comparison is the ordering boundary. Closing invalidates every admitted and in-flight call first.
        """
        admission = self._admit()
        if admission is None:
            return
        try:
            await self._manager.async_ensure_catalogue()
        except Exception:  # noqa: BLE001 -- a display need may never break a settings write
            _LOGGER.debug("market observation could not load the catalogue", exc_info=True)
            self._request_recovery(admission)
            return
        if not self._current(admission):
            return
        area_id = self._display_area(settings)
        if area_id is None:
            if self._area is not None:
                # No market named (or one nobody carries): stop asking for the old one, but only
                # the newest call may, so an obsolete one never releases a newer handle.
                self._release()
            elif settings.area_id:
                # An area is named but unresolvable (relay down or catalogue loading): transient,
                # so schedule the one recovery appointment.
                self._request_recovery(admission)
            return
        if self._area == area_id:
            self._cancel_recovery()
            return

        await self._adopt(admission, area_id)

    async def _adopt(self, admission: int, area_id: str) -> None:
        """Subscribe to [area_id] for [admission], publishing only while it is still current.

        Every attempt is its own manager owner (`<logical>#<admission>`): the manager's `(owner, area)` is
        one slot per owner, so an attempt completing after a newer one releases only its own slot, and the
        newest published owner is the only one that survives whatever the release order. Several owners on
        one area are still one upstream stream (the manager refcounts them). No lock is held across the
        subscribe.
        """

        def on_snapshot(_snapshot: Any) -> None:
            if not self._current(admission):
                return
            self._observed_area = area_id

        attempt_owner = f"{self._owner_id}#{admission}"
        try:
            handle = await self._manager.async_subscribe(
                owner_id=attempt_owner, area_id=area_id, listener=on_snapshot
            )
        except Exception:  # noqa: BLE001 -- a display need may never break a settings write
            # Area not in the catalogue, or its zone unresolvable: settings and charge are kept;
            # there is just no market yet.
            _LOGGER.debug("market observation could not subscribe to %s", area_id, exc_info=True)
            self._request_recovery(admission)
            return
        if not self._current(admission):
            handle()
            return
        # The one place the published subscription changes, atomic (no await after the check). The
        # previous handle is released after the new one is published, by its own named owner.
        previous = self._unsubscribe
        self._unsubscribe = handle
        self._published_owner = attempt_owner
        self._area = area_id
        self._observed_area = None
        if previous is not None:
            previous()
        self._cancel_recovery()

    async def async_close(self) -> None:
        """Release the subscription and end this observation. Terminal, and once.

        The admission counter is bumped before the handle is released, so every admitted call and anything
        in flight is stale from this instant. A call that completes anyway releases the handle it created
        itself, which is how closed stays closed.
        """
        if self._closed:
            return
        self._closed = True
        self._admission += 1
        self._cancel_recovery()
        remove_listener = self._remove_catalogue_listener
        self._remove_catalogue_listener = None
        self._release()
        if remove_listener is not None:
            remove_listener()

    def _on_catalogue_changed(self, _snapshot: CatalogueSnapshot) -> None:
        """The manager's catalogue signal: wake a waiting observation, and nothing else.

        The fast half of recovery: an area that could not be resolved while the relay was down becomes
        resolvable without a downloader, a second cache or a settings edit. It acts only while a recovery is
        pending and nothing is subscribed, so the snapshot delivered on registration cannot start a
        redundant cycle.
        """
        if self._closed or self._area is not None or self._recovery_cancel is None:
            return
        self._run_recovery()

    def _request_recovery(self, admission: int) -> None:
        """Arrange one retry after a transient failure, replacing any earlier one.

        Admission-guarded: an obsolete attempt never schedules anything. One timer, replaced rather than
        multiplied, cancelled by a successful subscribe and by close.
        """
        if self._closed or not self._current(admission):
            return
        self._cancel_recovery()
        self._recovery_cancel = self._schedule_recovery(self._run_recovery)

    def _cancel_recovery(self) -> None:
        cancel = self._recovery_cancel
        self._recovery_cancel = None
        if cancel is not None:
            cancel()

    @callback
    def _run_recovery(self) -> None:
        self._cancel_recovery()
        if self._closed:
            return
        self._hass.async_create_task(self.async_reconcile(self._store.settings(self._entry_id)))

    def _admit(self) -> int | None:
        if self._closed:
            return None
        self._admission += 1
        return self._admission

    def _current(self, admission: int) -> bool:
        """Whether [admission] is still the newest admitted call and still open: the whole ordering rule,
        one synchronous comparison with no await inside.
        """
        return not self._closed and admission == self._admission

    def _display_area(self, settings: AutoSettings) -> str | None:
        area_id = settings.area_id
        if not area_id:
            return None
        return area_id if self._manager.catalogue_snapshot().area(area_id) is not None else None

    def _release(self) -> None:
        """Release the published subscription, once, and forget it.

        Fields are cleared before the handle is called (the manager may run listeners on its way out, and a
        half-cleared observation is how a release gets repeated). In-flight attempts release their own
        owners when they return.
        """
        handle = self._unsubscribe
        self._unsubscribe = None
        self._published_owner = None
        self._area = None
        self._observed_area = None
        if handle is not None:
            handle()
