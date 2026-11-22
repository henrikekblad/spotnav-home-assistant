"""A charger's plan under a person's "Later departure" (`AutoPlannerController._with_later_departure`): made for the
later departure in place of the one moved, as a departure dated that day and time, never stored in the departure's
settings; not where the departure ahead is another; and a write that sets another departure ends it on the site."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pytest

from custom_components.spotnav.planning import auto_controller

from .harness import Harness
from .relay import Clock, flat_day, hold_history, index_listing, serve, SE4

pytestmark = pytest.mark.usefixtures("offline_relay")

#: Thursday 2026-10-01, 20:00 in Stockholm (CEST, UTC+2); the daily departure Friday 08:00 local.
THURSDAY = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
FRIDAY_0800 = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
FRIDAY_1100 = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def thursday_evening(clock: Clock) -> None:
    clock.now = THURSDAY


def _publish(harness: Harness) -> None:
    transport = harness.transport
    today = date(2026, 10, 1)
    days = (today, today + timedelta(days=1))
    serve(transport, days=days)
    for day in days:
        transport.serve(transport.day_path(SE4, day), 200, flat_day(SE4, day, price=0.10))
    transport.serve("/v1/index.json", 200, index_listing(SE4, [day.isoformat() for day in days]))
    hold_history(harness.repository, to=today.isoformat(), cheap={})


class _Site:
    """The site's "Later departure" record for the charger, as the planner reads it."""

    def __init__(self, moved: tuple[datetime, datetime] | None) -> None:
        self.moved = moved
        self.dropped: list[str] = []


def _site(monkeypatch, moved: tuple[datetime, datetime] | None) -> _Site:
    site = _Site(moved)
    controller = auto_controller.AutoPlannerController
    monkeypatch.setattr(controller, "_later_departure", lambda self: site.moved)

    def drop(self) -> None:
        site.dropped.append(self.entry_id)
        site.moved = None

    monkeypatch.setattr(controller, "_drop_later_departure", drop)
    return site


async def test_the_plan_is_made_for_the_later_departure_and_the_settings_stay(harness: Harness, monkeypatch) -> None:
    _publish(harness)
    _site(monkeypatch, (FRIDAY_0800, FRIDAY_1100))
    controller = await harness.auto(departure=time(8, 0), requested_kwh=10.0)
    assert controller.snapshot().departure_at == FRIDAY_1100
    stored = harness.store.settings("entry-a")
    assert stored.departure == time(8, 0) and stored.departure_date is None


async def test_not_where_the_departure_ahead_is_another(harness: Harness, monkeypatch) -> None:
    _publish(harness)
    # Moved from Friday 07:00, which the settings do not make (08:00 is): the plan is the settings' own.
    _site(monkeypatch, (FRIDAY_0800 - timedelta(hours=1), FRIDAY_1100))
    controller = await harness.auto(departure=time(8, 0), requested_kwh=10.0)
    assert controller.snapshot().departure_at == FRIDAY_0800


async def test_a_write_that_sets_another_departure_ends_it(harness: Harness, monkeypatch) -> None:
    _publish(harness)
    site = _site(monkeypatch, (FRIDAY_0800, FRIDAY_1100))
    controller = await harness.auto(departure=time(8, 0), requested_kwh=10.0)
    # A write that leaves the departure as it is keeps it.
    await controller.async_apply_settings(mutate=lambda settings: replace(settings, requested_kwh=12.0))
    assert site.dropped == [] and controller.snapshot().departure_at == FRIDAY_1100
    await controller.async_apply_settings(mutate=lambda settings: replace(settings, departure=time(9, 0)))
    assert site.dropped == ["entry-a"]
    assert controller.snapshot().departure_at == FRIDAY_0800 + timedelta(hours=1)
