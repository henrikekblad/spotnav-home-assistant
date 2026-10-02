"""A dated departure ("Sunday 08:00"), closed loop: real repository, manager, settings store and controller.

The clock and the wire are the only doubles. Every scenario starts on a Thursday evening with the next
day's prices published (the relay publishes one day ahead), a departure on the Sunday after, and a
history profile the relay may or may not serve:

* the profile says Sunday night is cheaper: SpotNav waits and buys nothing now;
* the profile is flat: it plans on what is published, tonight;
* there is no profile: the same;
* the need is too large to wait: only what cannot wait is bought;
* a date that has gone by is ignored, and the next write forgets it;
* a clock-change weekend still lands the deadline on the Sunday wall time.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pytest

from custom_components.spotnav.planning.auto_settings import AutoSettingsError
from custom_components.spotnav.planning.status_compose import QUIET_PLANNING_REASONS

from .harness import Harness
from .relay import (
    Clock,
    flat_day,
    index_listing,
    profile_path,
    serve,
    serve_profile,
    SE4,
)

pytestmark = pytest.mark.usefixtures("offline_relay")

#: Thursday 2026-10-01, 20:00 in Stockholm (CEST, UTC+2).
THURSDAY = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
FRIDAY_DATE = date(2026, 10, 2)
SUNDAY = date(2026, 10, 4)

#: Sunday's night hours, in the profile's local weekday-hour.
SUNDAY_NIGHT = {(7, hour): 0.03 for hour in range(8)}


def _publish(harness: Harness, today: date, **profile: Any) -> None:
    """Today and tomorrow published, and (unless `profile` is `None`) a profile with Sunday night cheap."""
    transport = harness.transport
    tomorrow = today + timedelta(days=1)
    serve(transport, days=(today, tomorrow))
    for day in (today, tomorrow):
        transport.serve(transport.day_path(SE4, day), 200, flat_day(SE4, day, price=0.10))
    transport.serve(
        "/v1/index.json", 200, index_listing(SE4, [today.isoformat(), tomorrow.isoformat()])
    )
    if profile.get("missing"):
        return
    profile.setdefault("cheap", SUNDAY_NIGHT)
    profile.setdefault("generated", f"{today.isoformat()}T14:05:00+02:00")
    serve_profile(transport, to=today.isoformat(), **profile)


@pytest.fixture(autouse=True)
def thursday_evening(clock: Clock) -> None:
    clock.now = THURSDAY


async def test_a_cheaper_sunday_night_in_the_history_makes_it_wait_and_buy_nothing_now(
    harness: Harness,
) -> None:
    _publish(harness, date(2026, 10, 1))
    controller = await harness.auto(
        departure=time(8, 0), departure_date=SUNDAY, requested_kwh=10.0
    )

    snapshot = controller.snapshot()
    assert snapshot.state == "waiting_for_publication" and snapshot.reason == "waiting_for_history"
    assert snapshot.reason in QUIET_PLANNING_REASONS
    assert snapshot.price_wait == "waiting" and snapshot.must_buy_kwh == 0.0
    assert snapshot.proposal is not None and snapshot.proposal.has_plan is False
    history = snapshot.history
    assert history is not None and history.outcome == "wait"
    assert history.weekday == 7 and history.weeks == 4
    assert history.percent is not None and history.percent >= 60
    assert history.expected_mean_minor < history.known_mean_minor - history.margin_minor
    assert snapshot.applied is False


async def test_a_flat_history_plans_on_the_published_prices_tonight(harness: Harness) -> None:
    _publish(harness, date(2026, 10, 1), cheap={})
    controller = await harness.auto(
        departure=time(8, 0), departure_date=SUNDAY, requested_kwh=10.0
    )

    snapshot = controller.snapshot()
    assert snapshot.state == "proposal_ready" and snapshot.reason == "ready"
    assert snapshot.history is not None and snapshot.history.outcome == "plan_known"
    proposal = snapshot.proposal
    assert proposal is not None and proposal.has_plan and proposal.unpriced is False
    # Everything planned is a published interval: tonight or tomorrow, nothing from the weekend.
    assert proposal.slots[-1].end <= datetime(2026, 10, 3, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert snapshot.price_wait is None


async def test_without_a_profile_it_plans_on_the_published_prices(harness: Harness) -> None:
    _publish(harness, date(2026, 10, 1), missing=True)
    controller = await harness.auto(
        departure=time(8, 0), departure_date=SUNDAY, requested_kwh=10.0
    )

    snapshot = controller.snapshot()
    assert snapshot.state == "proposal_ready" and snapshot.reason == "ready"
    assert snapshot.history is not None and snapshot.history.outcome == "no_profile"
    assert snapshot.proposal is not None and snapshot.proposal.has_plan
    assert harness.transport.call_count(profile_path()) == 1


async def test_a_need_the_published_prices_cannot_hold_buys_only_what_cannot_wait(
    harness: Harness,
) -> None:
    """100 kWh at 2.3 kW is 43 hours; only 28 are published. The daily rule applies unchanged."""
    _publish(harness, date(2026, 10, 1))
    controller = await harness.auto(
        departure=time(8, 0), departure_date=SUNDAY, requested_kwh=100.0
    )

    snapshot = controller.snapshot()
    assert snapshot.state == "proposal_ready" and snapshot.reason == "buying_before_publication"
    assert snapshot.price_wait == "buy_now" and 0 < snapshot.must_buy_kwh < 100.0
    proposal = snapshot.proposal
    assert proposal is not None and proposal.has_plan
    assert proposal.requested_kwh == pytest.approx(snapshot.must_buy_kwh)
    assert all(end <= snapshot.publication_at for _, end in proposal.periods)


async def test_when_history_argues_for_waiting_only_what_cannot_wait_is_bought(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capacity rule is `price_wait.decide`'s; here it says 5 kWh cannot wait, and that is all that is bought."""
    from custom_components.spotnav.planning import auto_controller

    real = auto_controller.price_wait.decide

    def decide(**kwargs: Any) -> Any:
        decision = real(**kwargs)
        return replace(
            decision,
            action="buy_now",
            must_buy_kwh=5.0,
            window_end=decision.publication_at,
            eligible=(),
        )

    monkeypatch.setattr(auto_controller.price_wait, "decide", decide)
    _publish(harness, date(2026, 10, 1))
    controller = await harness.auto(
        departure=time(8, 0), departure_date=SUNDAY, requested_kwh=10.0
    )

    snapshot = controller.snapshot()
    assert snapshot.state == "proposal_ready" and snapshot.reason == "buying_before_publication"
    assert snapshot.history is not None and snapshot.history.outcome == "wait"
    assert snapshot.proposal is not None and snapshot.proposal.requested_kwh == pytest.approx(5.0)
    assert snapshot.must_buy_kwh == pytest.approx(5.0)


async def test_when_waiting_is_not_safe_history_is_set_aside_and_the_published_plan_stands(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.spotnav.planning import auto_controller

    real = auto_controller.price_wait.decide

    def decide(**kwargs: Any) -> Any:
        return replace(real(**kwargs), action="guarantee")

    monkeypatch.setattr(auto_controller.price_wait, "decide", decide)
    _publish(harness, date(2026, 10, 1))
    controller = await harness.auto(
        departure=time(8, 0), departure_date=SUNDAY, requested_kwh=10.0
    )

    snapshot = controller.snapshot()
    assert snapshot.state == "proposal_ready" and snapshot.reason == "ready"
    assert snapshot.history is not None and snapshot.history.outcome == "unsafe_to_wait"
    assert snapshot.proposal is not None and snapshot.proposal.unpriced is False


async def test_a_need_that_leaves_no_room_to_wait_is_planned_on_known_prices_whole(
    harness: Harness, clock: Clock
) -> None:
    """Late on Saturday the history still says Sunday night is cheaper, but waiting is no longer safe."""
    saturday = date(2026, 10, 3)
    clock.now = datetime(2026, 10, 3, 17, 30, tzinfo=timezone.utc)
    _publish(harness, saturday)
    # 10 kWh at 2.3 kW needs 4.3 hours at the full rate; the deadline is 12.5 hours away, but the
    # publication of Sunday's prices is only the next afternoon: no margin to wait for it.
    controller = await harness.auto(
        departure=time(8, 0), departure_date=saturday + timedelta(days=1), requested_kwh=10.0
    )
    snapshot = controller.snapshot()
    # Sunday is tomorrow here and published: nothing is unknown, nothing to weigh.
    assert snapshot.state == "proposal_ready" and snapshot.history is None


async def test_a_date_that_has_gone_by_is_ignored_by_planning_and_forgotten_by_the_next_write(
    harness: Harness, clock: Clock
) -> None:
    _publish(harness, date(2026, 10, 1))
    # Stored directly (a write refuses a past date): as if it had been set days ago.
    controller = await harness.auto(
        departure=time(8, 0), departure_date=date(2026, 9, 30), requested_kwh=10.0
    )
    snapshot = controller.snapshot()
    # Planned as a daily departure: tomorrow 08:00 is the deadline and nothing waits on history.
    assert snapshot.state == "proposal_ready" and snapshot.history is None
    assert snapshot.proposal is not None
    assert snapshot.proposal.slots[-1].end <= datetime(2026, 10, 2, 8, 0, tzinfo=timezone(timedelta(hours=2)))
    assert harness.store.settings("entry-a").departure_date == date(2026, 9, 30)

    await controller.async_apply_settings(mutate=lambda current: replace(current, amps=12))
    assert harness.store.settings("entry-a").departure_date is None


async def test_a_deadline_today_that_has_passed_is_the_daily_departure(
    harness: Harness, clock: Clock
) -> None:
    _publish(harness, date(2026, 10, 1))
    controller = await harness.auto(
        departure=time(8, 0), departure_date=date(2026, 10, 1), requested_kwh=10.0
    )
    # 08:00 today has gone by (it is 20:00): the daily rule takes tomorrow 08:00.
    assert controller.snapshot().history is None
    assert controller.snapshot().state == "proposal_ready"


async def test_the_departure_date_is_judged_at_every_write(harness: Harness) -> None:
    _publish(harness, date(2026, 10, 1))
    controller = await harness.auto(departure=time(8, 0))

    async def write(value: date | None) -> None:
        await controller.async_apply_settings(
            mutate=lambda current: replace(current, departure_date=value)
        )

    await write(date(2026, 10, 1))  # today
    await write(date(2026, 10, 8))  # today + 7
    await write(None)
    for refused in (date(2026, 9, 30), date(2026, 10, 9)):
        with pytest.raises(AutoSettingsError) as error:
            await write(refused)
        assert error.value.code == "invalid_departure"
    assert harness.store.settings("entry-a").departure_date is None


async def test_a_clock_change_weekend_still_puts_the_deadline_at_the_sunday_wall_time(
    harness: Harness, clock: Clock
) -> None:
    """Sunday 2026-10-25 has 25 hours: the repeated 02:00-03:00. 08:00 that day is 07:00 UTC."""
    thursday = date(2026, 10, 22)
    clock.now = datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc)
    _publish(harness, thursday)
    controller = await harness.auto(
        departure=time(8, 0), departure_date=date(2026, 10, 25), requested_kwh=10.0
    )
    snapshot = controller.snapshot()
    assert snapshot.history is not None and snapshot.history.outcome == "wait"
    gap_facts = snapshot.history
    # Sunday's unknown quarters: from Saturday 00:00 (the first unpublished) to Sunday 08:00 CET, which is
    # 24 + 25 - 16 = 33 elapsed hours before the deadline instant 07:00 UTC; the profile prices every one.
    start = datetime(2026, 10, 23, 22, 0, tzinfo=timezone.utc)
    deadline = datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)
    assert gap_facts.unknown_slots == int((deadline - start).total_seconds() // 900)
    # The wake-up is armed before the deadline's latest safe start, in absolute time.
    assert snapshot.price_wait in ("waiting", "buy_now")


async def test_every_publication_replans_and_the_published_weekend_decides(
    harness: Harness, clock: Clock
) -> None:
    """Waiting on Thursday; on Saturday the weekend is published and the plan is made on real prices."""
    from .relay import cheap_night_day

    _publish(harness, date(2026, 10, 1))
    controller = await harness.auto(
        departure=time(8, 0), departure_date=SUNDAY, requested_kwh=10.0
    )
    assert controller.snapshot().reason == "waiting_for_history"
    # The wake-up for the latest safe start is armed, in absolute time, before the deadline.
    assert harness.scheduler.pending

    # Saturday 14:00 local: Sunday's prices are out, and its night really is the cheapest of the window.
    clock.now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    transport = harness.transport
    saturday = date(2026, 10, 3)
    transport.serve(transport.day_path(SE4, saturday), 200, flat_day(SE4, saturday, price=0.10))
    transport.serve(transport.day_path(SE4, SUNDAY), 200, cheap_night_day(SE4, SUNDAY))
    transport.serve("/v1/index.json", 200, index_listing(SE4, [saturday.isoformat(), SUNDAY.isoformat()]))
    await harness.manager.async_refresh_area(SE4)
    await harness.hass.async_block_till_done()

    snapshot = controller.snapshot()
    assert snapshot.state == "proposal_ready" and snapshot.reason == "ready"
    assert snapshot.history is None, "nothing is unknown any more: no weighing, no waiting"
    proposal = snapshot.proposal
    assert proposal is not None and proposal.has_plan and proposal.unpriced is False
    stockholm = timezone(timedelta(hours=2))
    assert proposal.slots[0].start >= datetime(2026, 10, 4, 0, 0, tzinfo=stockholm)
    assert proposal.slots[-1].end <= datetime(2026, 10, 4, 8, 0, tzinfo=stockholm)


async def test_a_daily_departure_never_asks_for_history(harness: Harness) -> None:
    _publish(harness, date(2026, 10, 1))
    controller = await harness.auto(departure=time(8, 0), requested_kwh=10.0)

    snapshot = controller.snapshot()
    assert snapshot.history is None and snapshot.state == "proposal_ready"
    assert harness.transport.call_count(profile_path()) == 0


async def test_a_profile_is_asked_for_once_a_day_however_often_the_plan_is_recalculated(
    harness: Harness,
) -> None:
    _publish(harness, date(2026, 10, 1))
    controller = await harness.auto(
        departure=time(8, 0), departure_date=SUNDAY, requested_kwh=10.0
    )
    for _ in range(3):
        await controller.async_recalculate()
    assert harness.transport.call_count(profile_path()) == 1


async def test_a_dated_departure_is_the_same_instant_for_the_delivered_energy_baseline(
    harness: Harness,
) -> None:
    _publish(harness, date(2026, 10, 1))
    controller = await harness.auto(
        departure=time(8, 0), departure_date=SUNDAY, requested_kwh=10.0
    )
    settings = harness.store.settings("entry-a")
    entry = harness.manager.area_snapshot(SE4).catalogue
    assert controller._departure_key(settings, THURSDAY, entry).startswith("2026-10-04T08:00:00")
    daily = replace(settings, departure_date=None)
    assert controller._departure_key(daily, THURSDAY, entry).startswith("2026-10-02T08:00:00")


async def test_diagnostics_carry_the_profile_summary_and_the_last_wait_decision(
    hass: Any, harness: Harness
) -> None:
    from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics

    from .test_auto_controller import auto_section, registered_entry

    entry = registered_entry(hass)
    _publish(harness, date(2026, 10, 1))
    await harness.auto(departure=time(8, 0), departure_date=SUNDAY, requested_kwh=10.0)

    section = await auto_section(hass, entry)
    assert section["departure_date"] == "2026-10-04"
    decision = section["history_wait"]
    assert decision["outcome"] == "wait" and decision["weekday"] == 7 and decision["weeks"] == 4
    assert decision["expected_mean_minor"] < decision["known_mean_minor"] - decision["margin_minor"]
    assert decision["margin_minor"] > 0 and decision["percent"] == 70

    price_data = (await async_get_config_entry_diagnostics(hass, entry))["price_data"]
    profile = next(area for area in price_data["areas"] if area["area"] == SE4)["history_profile"]
    assert profile["usable"] is True and profile["weeks"] == 4 and profile["hour_entries"] == 168
    assert profile["from"] == "2026-09-04" and profile["to"] == "2026-10-01"
    assert not any(isinstance(value, (list, dict)) for value in profile.values()), "no price rows"


async def test_a_charger_that_never_weighed_history_reports_nothing_for_it(
    hass: Any, harness: Harness
) -> None:
    from .test_auto_controller import auto_section, registered_entry

    entry = registered_entry(hass)
    _publish(harness, date(2026, 10, 1))
    await harness.auto(departure=time(8, 0), requested_kwh=10.0)
    section = await auto_section(hass, entry)
    assert section["history_wait"] is None and section["departure_date"] is None
