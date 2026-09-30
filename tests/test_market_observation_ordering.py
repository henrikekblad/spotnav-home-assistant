"""The display observation's reconcile order: the newest admitted call owns the subscription.

Two settings changes can overlap -- the second starts while the first is still waiting for the
catalogue, or still inside the manager's own subscribe -- and the awaits are what make that
dangerous. Without an order the *older* call can finish last and leave the subscription on the
area the newer one has already moved away from, or subscribe to anything at all after the entry
has closed.

`DeferredManager` below is a test double, not a production seam: the ordering rule lives across
the awaits inside `MarketObservation.async_reconcile`, so a test has to be able to hold one call
inside an await while another call runs to completion. Everything else is the real manager, the
real repository and the real clock double.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.pricing.market_observation import MarketObservation
from custom_components.spotnav.pricing.price_refresh import PriceRefreshManager
from custom_components.spotnav.pricing.price_repository import PriceRepository

from tests.relay import FakeScheduler
from tests.relay import BASE_URL, Clock, StoreDouble, StubTransport

SE4 = "SE4"
DE_LU = "DE-LU"
DK1 = "DK1"


@dataclass
class SettingsStub:
    area_id: str | None


class StoreStub:
    def __init__(self, settings: SettingsStub) -> None:
        self._settings = settings

    def settings(self, _entry_id: str) -> Any:
        return self._settings


class DeferredManager:
    """The real manager with the two awaits of a reconcile made deferrable, per call."""

    def __init__(self, manager: PriceRefreshManager) -> None:
        self.manager = manager
        self.catalogue_gates: list[asyncio.Event] = []
        self.subscribe_gates: list[asyncio.Event] = []

    def hold_catalogue(self) -> asyncio.Event:
        gate = asyncio.Event()
        self.catalogue_gates.append(gate)
        return gate

    def hold_subscribe(self) -> asyncio.Event:
        gate = asyncio.Event()
        self.subscribe_gates.append(gate)
        return gate

    async def async_ensure_catalogue(self) -> None:
        if self.catalogue_gates:
            await self.catalogue_gates.pop(0).wait()
        await self.manager.async_ensure_catalogue()

    def catalogue_snapshot(self) -> Any:
        return self.manager.catalogue_snapshot()

    def add_catalogue_listener(self, listener: Any) -> Any:
        return self.manager.add_catalogue_listener(listener)

    async def async_subscribe(self, **kwargs: Any) -> Any:
        if self.subscribe_gates:
            await self.subscribe_gates.pop(0).wait()
        return await self.manager.async_subscribe(**kwargs)


@pytest.fixture
def repository(hass: HomeAssistant, transport: StubTransport, clock: Clock) -> PriceRepository:
    return PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=StoreDouble())


@pytest.fixture
def manager(hass: HomeAssistant, repository: PriceRepository, clock: Clock) -> PriceRefreshManager:
    return PriceRefreshManager(hass, repository, now=clock, jitter=lambda: 0.5, scheduler=FakeScheduler())


def observation_for(
    hass: HomeAssistant, deferred: DeferredManager, area_id: str | None
) -> MarketObservation:
    return MarketObservation(hass, "entry_a", StoreStub(SettingsStub(area_id)), deferred)  # type: ignore[arg-type]


def day_calls(transport: StubTransport, area: str) -> list[str]:
    marker = f"/v1/{area}/"
    return [url for url in transport.calls if marker in url]


async def test_the_newest_admitted_reconcile_owns_the_area_when_the_older_finishes_last(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    """SE4 is admitted first, NO1 second: NO1 owns the subscription, whatever finishes first."""
    transport.serve_area(SE4)
    transport.serve_area(DE_LU)
    deferred = DeferredManager(manager)
    observation = observation_for(hass, deferred, SE4)

    gate = deferred.hold_catalogue()
    older = hass.async_create_task(observation.async_reconcile(SettingsStub(SE4)))
    await asyncio.sleep(0)
    # The newer settings change runs to completion while the older call is still waiting.
    await observation.async_reconcile(SettingsStub(DE_LU))
    assert manager.owner_area(observation.published_owner) == DE_LU

    gate.set()
    await older

    assert manager.owner_area(observation.published_owner) == DE_LU, "the older call must not move it back"
    assert observation.subscribed_area == DE_LU
    assert observation.observed_area is None


async def test_closing_during_the_catalogue_wait_leaves_no_owner_behind(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    """A close while a reconcile waits for the catalogue ends it: nothing may subscribe after."""
    transport.serve_area(SE4)
    deferred = DeferredManager(manager)
    observation = observation_for(hass, deferred, SE4)

    gate = deferred.hold_catalogue()
    waiting = hass.async_create_task(observation.async_reconcile(SettingsStub(SE4)))
    await asyncio.sleep(0)
    await observation.async_close()
    gate.set()
    await waiting
    await hass.async_block_till_done()

    assert observation.closed
    assert observation.subscribed_area is None
    assert manager.owner_area(observation.published_owner) is None, "no owner may appear after a close"


async def test_closing_during_the_subscribe_releases_the_handle_it_created(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    """A subscribe that completes after the close is released immediately, not published."""
    transport.serve_area(SE4)
    deferred = DeferredManager(manager)
    observation = observation_for(hass, deferred, SE4)

    gate = deferred.hold_subscribe()
    waiting = hass.async_create_task(observation.async_reconcile(SettingsStub(SE4)))
    await asyncio.sleep(0)
    await observation.async_close()
    gate.set()
    await waiting
    await hass.async_block_till_done()

    assert manager.owner_area(observation.published_owner) is None, "the handle it created was released"
    assert observation.subscribed_area is None


async def test_an_obsolete_subscription_neither_replaces_nor_clears_the_newer_handle(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    """The older call's handle is its own: the newer area's subscription survives it."""
    transport.serve_area(SE4)
    transport.serve_area(DE_LU)
    deferred = DeferredManager(manager)
    observation = observation_for(hass, deferred, SE4)

    gate = deferred.hold_subscribe()
    older = hass.async_create_task(observation.async_reconcile(SettingsStub(SE4)))
    await asyncio.sleep(0)
    await observation.async_reconcile(SettingsStub(DE_LU))
    assert manager.owner_area(observation.published_owner) == DE_LU

    gate.set()
    await older

    assert manager.owner_area(observation.published_owner) == DE_LU, "the newer handle is untouched"
    assert observation.subscribed_area == DE_LU
    # And the object is still coherent: closing releases the newer subscription, once.
    await observation.async_close()
    assert manager.owner_area(observation.published_owner) is None


async def test_a_reconcile_for_the_area_already_watched_changes_nothing(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    """The newest call for the same area is idempotent: same owner, no second fetch, no release."""
    transport.serve_area(SE4)
    deferred = DeferredManager(manager)
    observation = observation_for(hass, deferred, SE4)

    await observation.async_reconcile(SettingsStub(SE4))
    await hass.async_block_till_done()
    calls = len(day_calls(transport, SE4))

    await observation.async_reconcile(SettingsStub(SE4))
    await hass.async_block_till_done()

    assert manager.owner_area(observation.published_owner) == SE4
    assert observation.subscribed_area == SE4
    assert len(day_calls(transport, SE4)) == calls, "no second upstream fetch for the same area"

async def test_three_areas_with_two_delayed_subscribes_converge_on_the_newest(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    """Three admitted areas, two delayed subscribes, adversarial completion order: DK1 wins.

    This is the case a one-hop repair could not survive: SE4 and DE-LU are both still inside
    `async_subscribe` when the newest area (DK1) publishes, and they complete *after* it -- DE-LU
    first, SE4 last. Every attempt owns its own manager slot, so each obsolete one releases only its
    own and the published DK1 owner is untouched by construction, in any order.
    """
    transport.serve_area(SE4)
    transport.serve_area(DE_LU)
    transport.serve_area(DK1)
    deferred = DeferredManager(manager)
    observation = observation_for(hass, deferred, SE4)

    # The submits are admitted in order, so the gates queue in the same order: SE4 then DE-LU.
    first_gate = deferred.hold_subscribe()
    second_gate = deferred.hold_subscribe()
    oldest = hass.async_create_task(observation.async_reconcile(SettingsStub(SE4)))
    await asyncio.sleep(0)
    middle = hass.async_create_task(observation.async_reconcile(SettingsStub(DE_LU)))
    await asyncio.sleep(0)
    await observation.async_reconcile(SettingsStub(DK1))
    assert manager.owner_area(observation.published_owner) == DK1, "the newest publishes first here"

    # The adversarial order: a *newer* obsolete attempt completes before an older one.
    second_gate.set()
    await middle
    first_gate.set()
    await oldest

    assert manager.owner_area("observation:entry_a#1") is None, "the oldest attempt freed its own slot"
    assert manager.owner_area("observation:entry_a#2") is None, "and so did the middle one"
    assert observation.published_owner == "observation:entry_a#3"
    assert manager.owner_area(observation.published_owner) == DK1, "exactly the newest owner survives"
    assert observation.subscribed_area == DK1

    # And closing removes that one owner, exactly once.
    published = observation.published_owner
    await observation.async_close()
    await observation.async_close()
    assert manager.owner_area(published) is None
    assert observation.published_owner is None
    assert observation.subscribed_area is None


async def test_close_while_several_attempts_are_outstanding_releases_every_handle(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    """Closing mid-flight: the published handle is released, and so is every attempt's own."""
    transport.serve_area(SE4)
    transport.serve_area(DE_LU)
    transport.serve_area(DK1)
    deferred = DeferredManager(manager)
    observation = observation_for(hass, deferred, SE4)

    first_gate = deferred.hold_subscribe()
    second_gate = deferred.hold_subscribe()
    oldest = hass.async_create_task(observation.async_reconcile(SettingsStub(SE4)))
    await asyncio.sleep(0)
    middle = hass.async_create_task(observation.async_reconcile(SettingsStub(DE_LU)))
    await asyncio.sleep(0)
    await observation.async_reconcile(SettingsStub(DK1))
    assert manager.owner_area(observation.published_owner) == DK1

    await observation.async_close()
    first_gate.set()
    second_gate.set()
    await oldest
    await middle
    await hass.async_block_till_done()

    for attempt in ("observation:entry_a#1", "observation:entry_a#2", "observation:entry_a#3"):
        assert manager.owner_area(attempt) is None, f"{attempt} must not survive the close"
    assert observation.published_owner is None
    assert observation.subscribed_area is None
    assert observation.observed_area is None, "and nothing resurrected an owner"
