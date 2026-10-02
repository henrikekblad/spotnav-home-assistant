"""A daily departure whose deadline lies beyond the last published interval, closed loop.

It is Friday 08:00 in Stockholm, only today is published, and the departure is 08:00 tomorrow (Saturday),
so Saturday's early hours are unpriced until the afternoon publication:

* the profile says Saturday night is cheaper: SpotNav waits, with the history status, and buys nothing;
* there is no profile: the implicit rule (wait for the publication) decides, as it always did;
* the published hours are clearly cheaper than the expected ones: it stops waiting and plans on them;
* a flat or missing profile, a need that cannot wait, or waiting that is no longer safe: the implicit rule;
* after the publication nothing is unknown and the published prices are planned.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from typing import Any

import pytest

from custom_components.spotnav.planning.status_compose import QUIET_PLANNING_REASONS

from .harness import Harness
from .relay import Clock, flat_day, index_listing, serve, serve_profile, SE4

pytestmark = pytest.mark.usefixtures("offline_relay")

#: Friday 2026-10-02, 08:00 in Stockholm (CEST, UTC+2).
FRIDAY_MORNING = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
FRIDAY = date(2026, 10, 2)
SATURDAY = date(2026, 10, 3)

#: Saturday's night hours (ISO weekday 6), in the profile's local weekday-hour.
SATURDAY_NIGHT = {(6, hour): 0.03 for hour in range(8)}


def _publish(harness: Harness, *, tomorrow: bool = False, **profile: Any) -> None:
    """Today (and optionally tomorrow) published; a profile with Saturday night cheap unless `missing`."""
    transport = harness.transport
    days = (FRIDAY, SATURDAY) if tomorrow else (FRIDAY,)
    serve(transport, days=days)
    for day in days:
        transport.serve(transport.day_path(SE4, day), 200, flat_day(SE4, day, price=0.10))
    transport.serve("/v1/index.json", 200, index_listing(SE4, [day.isoformat() for day in days]))
    if profile.get("missing"):
        return
    profile.setdefault("cheap", SATURDAY_NIGHT)
    profile.setdefault("generated", f"{FRIDAY.isoformat()}T07:05:00+02:00")
    serve_profile(transport, to=FRIDAY.isoformat(), **profile)


@pytest.fixture(autouse=True)
def friday_morning(clock: Clock) -> None:
    clock.now = FRIDAY_MORNING


async def test_a_cheaper_saturday_night_makes_a_daily_departure_wait_with_the_history_status(
    harness: Harness,
) -> None:
    _publish(harness)
    controller = await harness.auto(departure=time(8, 0), requested_kwh=10.0)

    snapshot = controller.snapshot()
    assert snapshot.state == "waiting_for_publication" and snapshot.reason == "waiting_for_history"
    assert snapshot.reason in QUIET_PLANNING_REASONS
    assert snapshot.price_wait == "waiting" and snapshot.must_buy_kwh == 0.0
    assert snapshot.wait_rule == "history"
    history = snapshot.history
    assert history is not None and history.outcome == "wait"
    assert history.weekday == 6 and history.weeks == 4
    assert history.expected_mean_minor < history.known_mean_minor - history.margin_minor
    assert snapshot.proposal is not None and snapshot.proposal.has_plan is False
    assert snapshot.applied is False


async def test_a_flat_history_keeps_the_implicit_wait_for_the_publication(harness: Harness) -> None:
    """Waiting for the publication is nearly free, so a flat profile must not turn it into buying now."""
    _publish(harness, cheap={})
    controller = await harness.auto(departure=time(8, 0), requested_kwh=10.0)

    snapshot = controller.snapshot()
    assert snapshot.state == "waiting_for_publication" and snapshot.reason == "publication_pending"
    assert snapshot.price_wait == "waiting" and snapshot.wait_rule == "implicit"
    assert snapshot.history is None


async def test_a_dearer_saturday_night_ends_the_wait_and_plans_on_the_published_prices(
    harness: Harness,
) -> None:
    _publish(harness, cheap={(6, hour): 0.30 for hour in range(8)})
    controller = await harness.auto(departure=time(8, 0), requested_kwh=10.0)

    snapshot = controller.snapshot()
    assert snapshot.state == "proposal_ready" and snapshot.reason == "ready"
    assert snapshot.wait_rule == "history" and snapshot.price_wait is None
    history = snapshot.history
    assert history is not None and history.known_cheaper is True
    assert history.known_mean_minor < history.expected_mean_minor - history.margin_minor
    proposal = snapshot.proposal
    assert proposal is not None and proposal.has_plan and proposal.unpriced is False
    assert proposal.slots[-1].end <= datetime(2026, 10, 3, 0, 0, tzinfo=timezone(timedelta(hours=2)))


async def test_without_a_profile_a_daily_departure_keeps_the_implicit_rule(harness: Harness) -> None:
    _publish(harness, missing=True)
    controller = await harness.auto(departure=time(8, 0), requested_kwh=10.0)

    snapshot = controller.snapshot()
    assert snapshot.state == "waiting_for_publication" and snapshot.reason == "publication_pending"
    assert snapshot.price_wait == "waiting" and snapshot.history is None
    assert snapshot.wait_rule == "implicit"


async def test_a_need_that_cannot_wait_buys_only_what_cannot_wait_by_the_implicit_rule(
    harness: Harness,
) -> None:
    """40 kWh cannot all wait: history agrees with waiting but the implicit capacity rule buys the rest."""
    _publish(harness)
    controller = await harness.auto(departure=time(8, 0), requested_kwh=40.0)

    snapshot = controller.snapshot()
    assert snapshot.state == "proposal_ready" and snapshot.reason == "buying_before_publication"
    assert snapshot.price_wait == "buy_now" and snapshot.wait_rule == "implicit"
    assert 0 < snapshot.must_buy_kwh < 40.0
    proposal = snapshot.proposal
    assert proposal is not None and proposal.has_plan and proposal.unpriced is False
    assert proposal.requested_kwh == pytest.approx(snapshot.must_buy_kwh)
    assert all(end <= snapshot.publication_at for _, end in proposal.periods)


async def test_when_waiting_is_not_safe_the_implicit_guarantee_stands(
    harness: Harness, clock: Clock
) -> None:
    """23:00, the publication overdue and a need that fills the night: charge now, as the implicit rule does."""
    clock.now = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)
    _publish(harness)
    controller = await harness.auto(departure=time(8, 0), requested_kwh=20.0)

    snapshot = controller.snapshot()
    assert snapshot.reason == "charging_without_prices" and snapshot.price_wait == "guarantee"
    assert snapshot.wait_rule == "implicit" and snapshot.history is None


async def test_after_the_publication_the_published_prices_are_planned(harness: Harness) -> None:
    _publish(harness, tomorrow=True)
    controller = await harness.auto(departure=time(8, 0), requested_kwh=10.0)

    snapshot = controller.snapshot()
    assert snapshot.state == "proposal_ready" and snapshot.history is None
    assert snapshot.wait_rule is None and snapshot.price_wait is None
    assert snapshot.proposal is not None and snapshot.proposal.has_plan
