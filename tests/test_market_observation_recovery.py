"""Recovery: a transient failure must not end the observation for the rest of the day.

The display need is a *background* need -- nobody is waiting for it, and no settings edit comes to
rescue it -- so a catalogue or subscribe failure that simply returned would leave the dashboard
empty until the person happened to change something. Instead the failure leaves **one** cancellable
appointment behind, and the manager's own catalogue signal can wake the observation sooner. Both
are exercised here with a recording scheduler and a real manager, repository and transport.

What is deliberately *not* here: another downloader, another price cache, or a retry of an area from
a reconcile that has been superseded. Recovery re-reads the store, so it always asks for the area the
charger has *now*.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.pricing.market_observation import MarketObservation
from custom_components.spotnav.pricing.price_refresh import PriceRefreshManager
from custom_components.spotnav.pricing.price_repository import PriceRepository

from tests.relay import FakeScheduler
from tests.relay import BASE_URL, TODAY, Clock, StoreDouble, StubTransport

SE4 = "SE4"
DE_LU = "DE-LU"


@dataclass
class SettingsStub:
    area_id: str | None


class StoreStub:
    """The one fact the observation reads, and mutable, so a settings move can be simulated."""

    def __init__(self, settings: SettingsStub) -> None:
        self.value = settings

    def settings(self, _entry_id: str) -> Any:
        return self.value


class FakeRecoveryScheduler:
    """One-shot appointments, recorded instead of waited for."""

    def __init__(self) -> None:
        self.live: list[Callable[[], None]] = []
        self.cancelled = 0

    def __call__(self, action: Callable[[], None]) -> Callable[[], None]:
        self.live.append(action)

        def cancel() -> None:
            if action in self.live:
                self.live.remove(action)
                self.cancelled += 1

        return cancel

    def fire(self) -> None:
        if not self.live:
            return
        action = self.live.pop()
        action()


@pytest.fixture
def repository(hass: HomeAssistant, transport: StubTransport, clock: Clock) -> PriceRepository:
    return PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=StoreDouble())


@pytest.fixture
def scheduler() -> FakeScheduler:
    return FakeScheduler()


@pytest.fixture
def manager(
    hass: HomeAssistant, repository: PriceRepository, clock: Clock, scheduler: FakeScheduler
) -> PriceRefreshManager:
    return PriceRefreshManager(hass, repository, now=clock, jitter=lambda: 0.5, scheduler=scheduler)


def failing_observation(
    hass: HomeAssistant, manager: PriceRefreshManager, store: StoreStub, scheduler: FakeRecoveryScheduler
) -> MarketObservation:
    return MarketObservation(
        hass, "entry_a", store, manager, schedule_recovery=scheduler  # type: ignore[arg-type]
    )


async def test_a_transient_catalogue_failure_recovers_without_another_settings_write(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager, repository: PriceRepository
) -> None:
    recovery = FakeRecoveryScheduler()
    store = StoreStub(SettingsStub(SE4))
    observation = failing_observation(hass, manager, store, recovery)

    # The relay is down: nothing is served, so the catalogue cannot be resolved.
    await observation.async_start()
    assert observation.subscribed_area is None
    assert observation.recovery_pending, "a transient failure leaves exactly one retry behind"

    # It comes back -- and nothing else happens: no settings edit, no reload, no new downloader.
    transport.serve_area(SE4)
    recovery.fire()
    await hass.async_block_till_done()

    assert observation.subscribed_area == SE4
    assert not observation.recovery_pending, "a successful subscribe cancels the appointment"
    assert recovery.live == []
    assert manager.owner_area(observation.published_owner) == SE4
    assert repository.day_snapshot(SE4, TODAY).document is not None


async def test_the_managers_own_catalogue_signal_recovers_it_too(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    recovery = FakeRecoveryScheduler()
    observation = failing_observation(hass, manager, StoreStub(SettingsStub(SE4)), recovery)
    await observation.async_start()
    assert observation.recovery_pending

    transport.serve_area(SE4)
    # The manager's own catalogue call fetches nothing on our behalf -- it is the *manager's* fetch --
    # and then hands the loaded list to its listeners: that signal is what this observation reuses.
    await manager.async_ensure_catalogue()
    await hass.async_block_till_done()

    assert observation.subscribed_area == SE4, "the catalogue arriving was enough"
    assert not observation.recovery_pending


async def test_a_settings_move_while_recovery_is_pending_subscribes_only_the_newest_area(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    recovery = FakeRecoveryScheduler()
    store = StoreStub(SettingsStub(SE4))
    observation = failing_observation(hass, manager, store, recovery)
    await observation.async_start()
    assert observation.recovery_pending

    # The person moves the charger while the relay is still down. Recovery is replaced, not multiplied,
    # and it will ask for what the store says *now*.
    store.value = SettingsStub(DE_LU)
    await observation.async_reconcile(store.settings("entry_a"))
    assert len(recovery.live) == 1, "one appointment, replaced rather than multiplied"

    transport.serve_area(SE4)
    transport.serve_area(DE_LU)
    recovery.fire()
    await hass.async_block_till_done()

    assert observation.subscribed_area == DE_LU, "only the newest area is subscribed"
    assert manager.owner_area("observation:entry_a#1") is None, "the obsolete area's attempt is gone"


async def test_closing_before_recovery_leaves_no_retry_and_no_owner(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    recovery = FakeRecoveryScheduler()
    observation = failing_observation(hass, manager, StoreStub(SettingsStub(SE4)), recovery)
    await observation.async_start()
    assert observation.recovery_pending

    await observation.async_close()
    assert not observation.recovery_pending, "close cancels the appointment"
    assert recovery.cancelled == 1

    transport.serve_area(SE4)
    recovery.fire()
    await hass.async_block_till_done()

    assert observation.subscribed_area is None, "a closed observation never subscribes"
    assert observation.published_owner is None


async def test_repeated_failure_signals_create_at_most_one_pending_recovery(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    recovery = FakeRecoveryScheduler()
    store = StoreStub(SettingsStub(SE4))
    observation = failing_observation(hass, manager, store, recovery)
    await observation.async_start()

    for _ in range(3):
        await observation.async_reconcile(store.settings("entry_a"))

    assert len(recovery.live) == 1, "however many failures, one appointment"
    assert recovery.cancelled >= 3, "each new one replaced the previous"


async def test_the_real_recovery_timer_retries_on_the_event_loop(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    """The production timer (no injected scheduler) must run the retry on the loop.

    A callback Home Assistant runs in a worker thread reaches `async_create_task` from there, which
    raises -- and the retry after a transient failure silently never happens.
    """
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.spotnav.pricing.market_observation import RECOVERY_DELAY_SECONDS

    observation = MarketObservation(hass, "entry_a", StoreStub(SettingsStub(SE4)), manager)
    await observation.async_start()
    assert observation.recovery_pending

    transport.serve_area(SE4)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RECOVERY_DELAY_SECONDS + 1))
    await hass.async_block_till_done()

    assert observation.subscribed_area == SE4
    assert not observation.recovery_pending
