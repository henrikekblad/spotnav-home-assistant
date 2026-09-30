"""The display observation: one charger's own area, kept fresh whatever the mode is.

The refresh manager is subscribed for display in every mode, not only `auto_price`, so the price
graph always has data.

* a charger keeps its area fresh in **every** mode, and only for an area the catalogue knows;
* one area is **one upstream stream** however many owners need it;
* the subscription **follows** the settings' area, **releases** on unload, and a reload leaves no
  second owner behind;
* an area the catalogue does not know is not observed at all.

The manager's own delivery guard is asserted in `test_price_refresh.py`; here it is the ownership
that makes the guard unreachable in practice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.pricing.market_observation import (
    MarketObservation,
)
from custom_components.spotnav.pricing.price_refresh import PriceRefreshManager
from custom_components.spotnav.pricing.price_repository import PriceRepository

from tests.relay import FakeScheduler
from tests.relay import BASE_URL, TODAY, Clock, StoreDouble, StubTransport

SE4 = "SE4"
DE_LU = "DE-LU"


@dataclass
class SettingsStub:
    """The one fact this object reads from a charger's settings: the area it names.

    A stub rather than a real `AutoSettings` on purpose: the observation's contract is "an object
    with an area", and the *real* settings path -- the store, a settings write and a real config
    entry -- is exercised end to end in `test_market_observation_dashboard.py`.
    """

    area_id: str | None
    mode: str = "external"


class StoreStub:
    def __init__(self, settings: SettingsStub) -> None:
        self._settings = settings

    def settings(self, _entry_id: str) -> Any:
        return self._settings


@pytest.fixture
def repository(hass: HomeAssistant, transport: StubTransport, clock: Clock) -> PriceRepository:
    return PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=StoreDouble())


@pytest.fixture
def manager(
    hass: HomeAssistant, repository: PriceRepository, clock: Clock
) -> PriceRefreshManager:
    return PriceRefreshManager(hass, repository, now=clock, jitter=lambda: 0.5, scheduler=FakeScheduler())


def observation_for(
    hass: HomeAssistant, manager: PriceRefreshManager, entry_id: str, area_id: str | None
) -> MarketObservation:
    return MarketObservation(hass, entry_id, StoreStub(SettingsStub(area_id)), manager)


def day_calls(transport: StubTransport, area: str) -> list[str]:
    """Every upstream day-document request this transport was asked for, for one area."""
    marker = f"/v1/{area}/"
    return [url for url in transport.calls if marker in url]


async def test_an_external_charger_keeps_its_area_fresh(hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager, repository: PriceRepository) -> None:
    """The defect itself: `external` needs the documents, so `external` subscribes."""
    transport.serve_area(SE4)
    observation = observation_for(hass, manager, "entry_a", SE4)

    await observation.async_start()
    await hass.async_block_till_done()

    assert observation.subscribed_area == SE4, "an external charger keeps its own area"
    assert manager.owner_area(observation.published_owner) == SE4
    snapshot = repository.day_snapshot(SE4, TODAY)
    assert snapshot.document is not None, "and the shared repository really holds the day"
    assert day_calls(transport, SE4), "one upstream fetch happened for it"


async def test_two_chargers_on_one_area_share_one_upstream_stream(hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager, repository: PriceRepository) -> None:
    """Two owners, one area, one fetch: subscribing is a refcount, not a downloader."""
    transport.serve_area(SE4)
    first = observation_for(hass, manager, "entry_a", SE4)
    second = observation_for(hass, manager, "entry_b", SE4)

    await first.async_start()
    await second.async_start()
    await hass.async_block_till_done()

    assert manager.owner_area(first.published_owner) == SE4
    assert manager.owner_area(second.published_owner) == SE4
    assert len(day_calls(transport, SE4)) == 1, "the area's day was fetched once, for both"
    assert repository.day_snapshot(SE4, TODAY).document is not None


async def test_a_calculation_owner_and_a_display_owner_share_the_area_and_either_can_leave(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager, repository: PriceRepository
) -> None:
    """The Auto preview and the display need are two owners of one stream, in both directions."""
    transport.serve_area(SE4)
    observation = observation_for(hass, manager, "entry_a", SE4)
    await observation.async_start()
    # The preview's own subscription, with its own owner id (see auto_controller).
    release_calculation = await manager.async_subscribe(owner_id="auto:entry_a", area_id=SE4, listener=None)
    await hass.async_block_till_done()

    assert manager.owner_area("auto:entry_a") == SE4
    assert len(day_calls(transport, SE4)) == 1, "still one stream for the area"

    # The calculation side leaves (the mode went to external): the graph keeps its stream.
    release_calculation()
    await hass.async_block_till_done()
    assert manager.owner_area("auto:entry_a") is None
    assert manager.owner_area(observation.published_owner) == SE4, "the display owner still holds it"
    assert repository.day_snapshot(SE4, TODAY).document is not None


async def test_moving_a_charger_moves_its_subscription_and_leaves_no_owner_behind(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    """A settings change that names another area moves this subscription with it."""
    transport.serve_area(SE4)
    transport.serve_area(DE_LU)
    observation = observation_for(hass, manager, "entry_a", SE4)
    await observation.async_start()
    assert observation.subscribed_area == SE4

    await observation.async_reconcile(SettingsStub(DE_LU))
    await hass.async_block_till_done()

    assert observation.subscribed_area == DE_LU, "the new area is the one it keeps fresh"
    assert manager.owner_area(observation.published_owner) == DE_LU
    assert observation.observed_area is None, "and nothing of the old area is mistaken for it"

    # Settings that name no area at all release it, and an unknown area is never observed.
    await observation.async_reconcile(SettingsStub(None))
    assert observation.subscribed_area is None
    assert manager.owner_area(observation.published_owner) is None
    await observation.async_reconcile(SettingsStub("XX-nothing"))
    assert observation.subscribed_area is None, "an area the catalogue does not know is not observed"
    assert observation.recovery_pending, "and it is retried, not written off until the next edit"

    await observation.async_close()
    assert not observation.recovery_pending, "close takes the appointment with it"


async def test_close_releases_the_area_and_a_reload_does_not_leak_a_second_owner(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    """Close releases; a reload builds a new observation -- one owner, not two."""
    transport.serve_area(SE4)
    store = StoreStub(SettingsStub(SE4))
    observation = MarketObservation(hass, "entry_a", store, manager)
    await observation.async_start()
    await hass.async_block_till_done()
    assert manager.owner_area(observation.published_owner) == SE4

    await observation.async_close()
    await hass.async_block_till_done()
    assert manager.owner_area(observation.published_owner) is None, "the unload released the area"

    again = MarketObservation(hass, "entry_a", store, manager)
    assert again is not observation, "a reload is a new object"
    await again.async_start()
    await hass.async_block_till_done()
    assert manager.owner_area(again.published_owner) == SE4


async def test_a_setup_that_cannot_reach_the_catalogue_is_still_a_working_charger(
    hass: HomeAssistant, transport: StubTransport, manager: PriceRefreshManager
) -> None:
    """No catalogue, no area: nothing is subscribed, and nothing is guessed."""
    observation = observation_for(hass, manager, "entry_a", SE4)
    await observation.async_start()

    assert observation.subscribed_area is None, "an unknowable area is not observed"
    assert manager.owner_area(observation.published_owner) is None
    assert observation.recovery_pending, "and the failure is not the end of the display need"

    await observation.async_close()
    assert not observation.recovery_pending, "close takes the appointment with it"
