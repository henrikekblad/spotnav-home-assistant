"""Price wait: the pure decision, and closed-loop days that prove it keeps the deadline.

The decision (`price_wait.decide`) is small; what makes it trustworthy is that a controller calling
it on every replan, against days whose prices arrive late or never, still finishes the charge by the
deadline and buys only at real prices -- the one exception being the guarantee, which says so. The
closed loop below is that controller in miniature: every quarter-hour it replans exactly the way
`auto_controller._plan_while_prices_are_missing` does (real planner, real `price_gap`, real
`price_wait.decide`), installs what it decides, and lets a simulated charger deliver energy.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pytest
from homeassistant.util import dt as dt_util

from custom_components.spotnav.planning import price_wait
from custom_components.spotnav.planning.planner import (
    PlanRequest,
    calculate_plan,
    departure_for,
    plan_unpriced,
    power_kw,
    price_gap,
)
from custom_components.spotnav.planning.price_wait import KnownInterval, decide, expected_publication_at
from custom_components.spotnav.pricing.relay_contract import PriceDocument, PriceInterval

UTC = timezone.utc
STOCKHOLM = "Europe/Stockholm"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


# ------------------------------------------------------------------ the pure decision


def hourly(start: datetime, hours: int) -> list[KnownInterval]:
    return [
        KnownInterval(start + timedelta(hours=index), start + timedelta(hours=index + 1), 0.1 * (index % 5))
        for index in range(hours)
    ]


NOW = at("2026-09-22T08:00:00+00:00")
DEADLINE = at("2026-09-23T06:00:00+00:00")  # 22 hours away
PUBLICATION = at("2026-09-22T11:45:00+00:00")  # 3.75 hours away


def test_a_need_that_fits_after_the_publication_waits() -> None:
    # After the publication 18.25 hours remain: at 2 kW x 0.8 that is 29.2 kWh, so 20 waits.
    decision = decide(
        now=NOW, deadline=DEADLINE, need_kwh=20.0, max_charge_kw=2.0,
        known=hourly(NOW, 16), publication_at=PUBLICATION,
    )
    assert decision.action == "wait" and decision.must_buy_kwh == 0.0
    assert decision.publication_at == PUBLICATION
    assert decision.latest_safe_start == DEADLINE - timedelta(hours=20.0 / 1.6)


def test_only_what_cannot_wait_is_bought_from_the_known_intervals_before_the_publication() -> None:
    decision = decide(
        now=NOW, deadline=DEADLINE, need_kwh=33.0, max_charge_kw=2.0,
        known=hourly(NOW, 16), publication_at=PUBLICATION,
    )
    assert decision.action == "buy_now"
    assert decision.must_buy_kwh == pytest.approx(33.0 - 18.25 * 1.6)
    assert decision.window_end == PUBLICATION
    assert decision.eligible and all(item.end <= PUBLICATION for item in decision.eligible)


def test_the_guarantee_is_due_one_slot_before_the_latest_safe_start() -> None:
    need, kw = 20.0, 2.0
    safe_start = DEADLINE - timedelta(hours=need / (kw * 0.8))
    just_before = safe_start - price_wait.START_LAG - timedelta(seconds=10)
    later = safe_start - price_wait.START_LAG + timedelta(seconds=10)

    early = decide(
        now=just_before, deadline=DEADLINE, need_kwh=need, max_charge_kw=kw,
        known=hourly(just_before, 3), publication_at=PUBLICATION,
    )
    due = decide(
        now=later, deadline=DEADLINE, need_kwh=need, max_charge_kw=kw,
        known=hourly(later, 3), publication_at=PUBLICATION,
    )

    assert early.action != "guarantee"
    assert due.action == "guarantee" and due.must_buy_kwh == need
    assert due.act_by == safe_start - price_wait.START_LAG


def test_a_need_exactly_at_the_wait_capacity_waits_despite_float_noise() -> None:
    """Break-even: equal is not "buy a sliver now"."""
    hours_after = (DEADLINE - PUBLICATION).total_seconds() / 3600
    exact = hours_after * 2.0 * 0.8
    for nudge in (0.0, 1e-9, -1e-9, 1e-7):
        decision = decide(
            now=NOW, deadline=DEADLINE, need_kwh=exact + nudge, max_charge_kw=2.0,
            known=hourly(NOW, 16), publication_at=PUBLICATION,
        )
        assert decision.action == "wait", nudge


def test_an_overdue_publication_is_measured_from_now_not_from_the_past() -> None:
    """The publication was due hours ago and has not come: the slack shrinks with the clock."""
    overdue = at("2026-09-22T05:00:00+00:00")
    soon = decide(
        now=NOW, deadline=DEADLINE, need_kwh=20.0, max_charge_kw=2.0,
        known=hourly(NOW, 16), publication_at=overdue,
    )
    assert soon.action == "wait", "22 hours x 1.6 kW holds 35 kWh"
    tight = decide(
        now=at("2026-09-23T00:00:00+00:00"), deadline=DEADLINE, need_kwh=20.0, max_charge_kw=2.0,
        known=hourly(NOW, 16), publication_at=overdue,
    )
    assert tight.action == "guarantee"


def test_known_prices_too_few_to_hold_the_purchase_fall_back_to_the_guarantee() -> None:
    decision = decide(
        now=NOW, deadline=DEADLINE, need_kwh=35.0, max_charge_kw=2.0,
        known=hourly(NOW, 1), publication_at=PUBLICATION,
    )
    assert decision.action == "guarantee", "a purchase that cannot fit would silently miss the deadline"


def test_a_decision_needs_something_to_charge() -> None:
    with pytest.raises(ValueError):
        decide(
            now=NOW, deadline=DEADLINE, need_kwh=0.0, max_charge_kw=2.0,
            known=[], publication_at=PUBLICATION,
        )


def test_publication_is_thirteen_hundred_brussels_plus_the_margin_on_the_day_before() -> None:
    assert expected_publication_at(date(2026, 9, 23)) == at("2026-09-22T11:45:00+00:00")  # CEST
    assert expected_publication_at(date(2026, 1, 15)) == at("2026-01-14T12:45:00+00:00")  # CET


def test_publication_is_dst_safe_across_both_clock_changes() -> None:
    # Spring: the clocks go forward on Sunday 2026-03-29. The day *after* it is published on the
    # 29th at 13:00 CEST (11:00Z); the transition day itself on the 28th at 13:00 CET (12:00Z).
    assert expected_publication_at(date(2026, 3, 29)) == at("2026-03-28T12:45:00+00:00")
    assert expected_publication_at(date(2026, 3, 30)) == at("2026-03-29T11:45:00+00:00")
    # Autumn: back on Sunday 2026-10-25.
    assert expected_publication_at(date(2026, 10, 25)) == at("2026-10-24T11:45:00+00:00")
    assert expected_publication_at(date(2026, 10, 26)) == at("2026-10-25T12:45:00+00:00")
    # Never a wall-clock subtraction: the gap between consecutive days' publications is 23 or 25 hours.
    across = expected_publication_at(date(2026, 3, 30)) - expected_publication_at(date(2026, 3, 29))
    assert across == timedelta(hours=23)
    across = expected_publication_at(date(2026, 10, 26)) - expected_publication_at(date(2026, 10, 25))
    assert across == timedelta(hours=25)


# ------------------------------------------------------------------ the closed loop


def day_document(day: date, *, tz: str = STOCKHOLM) -> PriceDocument:
    """A real day (23, 24 or 25 hours by instant): cheap night, dear morning, a cheap afternoon dip."""
    zone = dt_util.get_time_zone(tz)
    first = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(UTC)
    following = day + timedelta(days=1)
    last = datetime(following.year, following.month, following.day, tzinfo=zone).astimezone(UTC)
    count = int((last - first).total_seconds() // 900)
    intervals = []
    for index in range(count):
        start = first + timedelta(minutes=15 * index)
        hour = start.astimezone(zone).hour
        # A different shape per date so a plan cannot be right by accident of its day.
        price = {0: 0.08, 1: 0.05, 2: 0.04, 3: 0.04, 4: 0.06, 5: 0.09}.get(hour, 0.30)
        if 13 <= hour <= 15:
            price = 0.03 + 0.01 * (day.day % 3)
        if 17 <= hour <= 20:
            price = 0.55
        intervals.append(
            PriceInterval.from_instants(
                start, start + timedelta(minutes=15), zone=zone, eur_per_kwh=price
            )
        )
    return PriceDocument(
        version=1,
        area_id="SE4",
        day=day,
        tz=tz,
        unit="EUR/kWh",
        resolution_minutes=15,
        start=intervals[0].start,
        start_instant=first,
        published=None,
        retrieved=None,
        fx={},
        fx_date=None,
        fx_src=None,
        src=None,
        prices=tuple(interval.eur_per_kwh for interval in intervals),
        intervals=tuple(intervals),
    )


@dataclass
class Outcome:
    delivered_kwh: float = 0.0
    finished_at: datetime | None = None
    priced_slots: int = 0
    unpriced_slots: int = 0
    #: A slot the loop charged in whose price the plan had no published figure at the time.
    slots_without_a_price_when_planned: int = 0
    decisions: list[str] = field(default_factory=list)
    first_charge: datetime | None = None


def run_day(
    *,
    start: datetime,
    deadline_local: time,
    need_kwh: float,
    amps: int,
    phases: int,
    prices_arrive_at: datetime | None,
    today: date,
    tomorrow: date,
    tz: str = STOCKHOLM,
    actual_rate: float = 1.0,
    max_periods: int = 4,
) -> tuple[Outcome, datetime]:
    """Replan every quarter-hour until the need is met or the deadline passes.

    `actual_rate` is the share of the planned power the car really draws (the feasibility margin
    exists for cars that draw less). Returns the outcome and the deadline instant.
    """
    zone = dt_util.get_time_zone(tz)
    documents_today = day_document(today, tz=tz)
    documents_tomorrow = day_document(tomorrow, tz=tz)
    kw = power_kw(amps, phases)
    slot = timedelta(minutes=15)
    outcome = Outcome()
    installed: list[tuple[datetime, datetime, bool]] = []  # (start, end, priced)
    now = start
    # Fixed once, at the start: the person's departure is one instant, not "the next 08:00" again on
    # every replan (which would quietly move the goalposts after a miss).
    deadline = departure_for((documents_today,), now=start, tz=tz, departure=deadline_local)
    while True:
        published = prices_arrive_at is not None and now >= prices_arrive_at
        documents = (documents_today, documents_tomorrow) if published else (documents_today,)
        remaining = need_kwh - outcome.delivered_kwh
        if remaining <= 1e-9:
            outcome.finished_at = now
            return outcome, deadline
        if now >= deadline:
            return outcome, deadline  # a miss: `finished_at` stays `None`
        request = PlanRequest(
            area_id="SE4", timezone=tz, currency="EUR", major_unit="EUR", minor_unit="cent",
            documents=documents, now=now, phases=phases, amps=amps, requested_kwh=remaining,
            consumption_kwh_per_10km=2.0, max_periods=max_periods, departure=deadline_local,
        )
        result = calculate_plan(request)
        decision_name = "plan"
        if result.reason == "insufficient_price_horizon":
            gap = price_gap(request)
            assert gap is not None
            assert gap.deadline == deadline, "the gap and the loop agree on the deadline"
            missing_day = gap.missing_from.astimezone(zone).date()
            decision = decide(
                now=now,
                deadline=gap.deadline,
                need_kwh=remaining,
                max_charge_kw=kw,
                known=[
                    KnownInterval(known.start, known.start + slot, known.local_major_per_kwh)
                    for known in gap.known
                ],
                publication_at=expected_publication_at(missing_day),
            )
            decision_name = decision.action
            if decision.action == "buy_now":
                bought = calculate_plan(
                    replace(request, requested_kwh=decision.must_buy_kwh, window_end=decision.window_end)
                )
                if bought.has_plan:
                    installed = [(a, b, True) for a, b in bought.periods]
                else:
                    decision_name = "guarantee"
                    installed = [(a, b, False) for a, b in plan_unpriced(request).periods]
            elif decision.action == "guarantee":
                installed = [(a, b, False) for a, b in plan_unpriced(request).periods]
            # "wait" installs nothing new: whatever was installed stays, exactly as the executor leaves it.
        elif result.has_plan:
            installed = [(a, b, True) for a, b in result.periods]
        outcome.decisions.append(decision_name)
        # One quarter-hour of the simulated charger.
        for begin, end, priced in installed:
            if begin <= now < end:
                outcome.delivered_kwh += kw * actual_rate * 0.25
                if priced:
                    outcome.priced_slots += 1
                else:
                    outcome.unpriced_slots += 1
                if outcome.first_charge is None:
                    outcome.first_charge = now
                break
        now += slot


TODAY = date(2026, 9, 22)
TOMORROW = date(2026, 9, 23)
MORNING = at("2026-09-22T09:00:00+02:00")
EIGHT = time(8, 0)


def test_prices_at_13_05_are_waited_for_and_the_whole_charge_uses_real_prices() -> None:
    outcome, deadline = run_day(
        start=MORNING, deadline_local=EIGHT, need_kwh=40.0, amps=16, phases=3,
        prices_arrive_at=at("2026-09-22T13:05:00+02:00"), today=TODAY, tomorrow=TOMORROW,
    )
    assert deadline == at("2026-09-23T08:00:00+02:00")
    assert outcome.delivered_kwh >= 40.0 - 1e-9, "the need is met"
    assert outcome.finished_at is not None and outcome.finished_at <= deadline
    assert outcome.unpriced_slots == 0, "only real prices"
    assert set(outcome.decisions[: 4 * 4 + 1]) == {"wait"}, "nothing is planned before 13:05"
    assert outcome.first_charge is not None and outcome.first_charge >= at("2026-09-22T13:05:00+02:00")


def test_prices_at_16_00_change_nothing_but_the_time_the_plan_is_made() -> None:
    outcome, deadline = run_day(
        start=MORNING, deadline_local=EIGHT, need_kwh=40.0, amps=16, phases=3,
        prices_arrive_at=at("2026-09-22T16:00:00+02:00"), today=TODAY, tomorrow=TOMORROW,
    )
    assert outcome.delivered_kwh >= 40.0 - 1e-9
    assert outcome.finished_at is not None and outcome.finished_at <= deadline
    assert outcome.unpriced_slots == 0
    assert outcome.first_charge is not None and outcome.first_charge >= at("2026-09-22T16:00:00+02:00")
    assert "guarantee" not in outcome.decisions


@pytest.mark.parametrize("actual_rate", [1.0, 0.9])
def test_prices_that_never_come_are_guaranteed_at_the_latest_safe_start(actual_rate: float) -> None:
    outcome, deadline = run_day(
        start=MORNING, deadline_local=EIGHT, need_kwh=40.0, amps=16, phases=3,
        prices_arrive_at=None, today=TODAY, tomorrow=TOMORROW, actual_rate=actual_rate,
    )
    assert outcome.delivered_kwh >= 40.0 - 1e-9, "the deadline is kept even with no prices at all"
    assert outcome.finished_at is not None and outcome.finished_at <= deadline
    assert outcome.decisions.index("guarantee") > 0
    assert set(outcome.decisions[: outcome.decisions.index("guarantee")]) == {"wait"}, (
        "it waits, and never buys at unknown prices before it must"
    )
    assert outcome.unpriced_slots > 0 and outcome.priced_slots == 0
    # It started no earlier than the latest safe start less one slot, and no later than it.
    kw = power_kw(16, 3)
    safe_start = deadline - timedelta(hours=40.0 / (kw * 0.8))
    assert outcome.first_charge is not None
    assert safe_start - timedelta(minutes=30) <= outcome.first_charge <= safe_start + timedelta(minutes=15)


def test_a_need_too_large_to_wait_buys_only_the_excess_before_the_publication() -> None:
    """2.3 kW single phase, 40 kWh, deadline 08:00: 18.25 hours after 13:45 hold 33.6 kWh, so ~6.4 must
    be bought before then -- from published prices -- and the rest is planned on the real ones."""
    outcome, deadline = run_day(
        start=MORNING, deadline_local=EIGHT, need_kwh=40.0, amps=10, phases=1,
        prices_arrive_at=at("2026-09-22T13:05:00+02:00"), today=TODAY, tomorrow=TOMORROW,
    )
    assert outcome.decisions[0] == "buy_now"
    assert outcome.delivered_kwh >= 40.0 - 1e-9
    assert outcome.finished_at is not None and outcome.finished_at <= deadline
    assert outcome.unpriced_slots == 0, "the purchase was made in known prices"
    assert outcome.first_charge is not None and outcome.first_charge < at("2026-09-22T13:05:00+02:00"), (
        "the excess really was bought before the prices arrived"
    )


def test_a_need_too_large_to_wait_and_prices_that_never_come_still_meets_the_deadline() -> None:
    outcome, deadline = run_day(
        start=MORNING, deadline_local=EIGHT, need_kwh=40.0, amps=10, phases=1,
        prices_arrive_at=None, today=TODAY, tomorrow=TOMORROW,
    )
    assert outcome.delivered_kwh >= 40.0 - 1e-9
    assert outcome.finished_at is not None and outcome.finished_at <= deadline
    assert "buy_now" in outcome.decisions and "guarantee" in outcome.decisions
    assert outcome.priced_slots > 0, "what could be bought in known prices was"


def test_the_autumn_clock_change_day_keeps_the_deadline_and_the_real_prices() -> None:
    """25 October 2026: the night has 25 hours. Published (13:05 CEST the day before) or never."""
    today, tomorrow = date(2026, 10, 24), date(2026, 10, 25)
    start = at("2026-10-24T09:00:00+02:00")
    published, deadline = run_day(
        start=start, deadline_local=EIGHT, need_kwh=45.0, amps=16, phases=3,
        prices_arrive_at=at("2026-10-24T13:05:00+02:00"), today=today, tomorrow=tomorrow,
    )
    never, never_deadline = run_day(
        start=start, deadline_local=EIGHT, need_kwh=45.0, amps=16, phases=3,
        prices_arrive_at=None, today=today, tomorrow=tomorrow,
    )
    assert deadline == never_deadline == at("2026-10-25T08:00:00+01:00"), "08:00 is CET, an hour later"
    for outcome in (published, never):
        assert outcome.delivered_kwh >= 45.0 - 1e-9
        assert outcome.finished_at is not None and outcome.finished_at <= deadline
    assert published.unpriced_slots == 0 and never.unpriced_slots > 0


def test_the_spring_clock_change_day_keeps_the_deadline_and_the_real_prices() -> None:
    """29 March 2026: the night has 23 hours, so the guarantee comes an hour earlier in the wall clock."""
    today, tomorrow = date(2026, 3, 28), date(2026, 3, 29)
    start = at("2026-03-28T09:00:00+01:00")
    published, deadline = run_day(
        start=start, deadline_local=EIGHT, need_kwh=45.0, amps=16, phases=3,
        prices_arrive_at=at("2026-03-28T13:05:00+01:00"), today=today, tomorrow=tomorrow,
    )
    never, never_deadline = run_day(
        start=start, deadline_local=EIGHT, need_kwh=45.0, amps=16, phases=3,
        prices_arrive_at=None, today=today, tomorrow=tomorrow,
    )
    assert deadline == never_deadline == at("2026-03-29T08:00:00+02:00"), "08:00 is CEST, an hour earlier"
    for outcome in (published, never):
        assert outcome.delivered_kwh >= 45.0 - 1e-9
        assert outcome.finished_at is not None and outcome.finished_at <= deadline
    assert published.unpriced_slots == 0 and never.unpriced_slots > 0


def test_a_tiny_need_after_the_publication_waits_all_the_way_and_never_buys_early() -> None:
    outcome, deadline = run_day(
        start=MORNING, deadline_local=EIGHT, need_kwh=5.0, amps=16, phases=3,
        prices_arrive_at=at("2026-09-22T13:05:00+02:00"), today=TODAY, tomorrow=TOMORROW,
    )
    assert outcome.delivered_kwh >= 5.0 - 1e-9 and outcome.unpriced_slots == 0
    assert "buy_now" not in outcome.decisions
    assert outcome.first_charge is not None and outcome.first_charge >= at("2026-09-22T13:05:00+02:00")


def test_the_loop_itself_is_sound_a_day_that_is_already_priced_is_just_planned() -> None:
    """Sanity check of the harness: with both days published from the start nothing waits."""
    outcome, deadline = run_day(
        start=MORNING, deadline_local=EIGHT, need_kwh=40.0, amps=16, phases=3,
        prices_arrive_at=MORNING, today=TODAY, tomorrow=TOMORROW,
    )
    assert set(outcome.decisions) == {"plan"}
    assert outcome.delivered_kwh >= 40.0 - 1e-9 and outcome.unpriced_slots == 0


def test_the_planner_only_ever_chooses_published_prices() -> None:
    """Even asked for a window that runs past the published day, the planner names the gap."""
    request = PlanRequest(
        area_id="SE4", timezone=STOCKHOLM, currency="EUR", major_unit="EUR", minor_unit="cent",
        documents=(day_document(TODAY),), now=MORNING, phases=3, amps=16, requested_kwh=40.0,
        consumption_kwh_per_10km=2.0, max_periods=4, departure=EIGHT,
    )
    assert calculate_plan(request).reason == "insufficient_price_horizon"
    gap = price_gap(request)
    assert gap is not None and gap.missing_from == at("2026-09-23T00:00:00+02:00")
    assert gap.deadline == at("2026-09-23T08:00:00+02:00")
    assert gap.known[0].start == MORNING and gap.known[-1].start == at("2026-09-22T23:45:00+02:00")
    assert plan_unpriced(request).slots[0].source == "unpriced"


def test_the_estimated_fallback_is_gone_from_the_planner_request() -> None:
    fields: dict[str, Any] = {field_.name for field_ in PlanRequest.__dataclass_fields__.values()}
    assert "allow_estimated_fallback" not in fields
    assert "window_end" in fields


@pytest.mark.parametrize("amps,phases", [(10, 1), (16, 3), (32, 3)])
@pytest.mark.parametrize("arrival", [None, "13:05", "16:00", "22:00", "03:05+1"])
def test_every_feasible_need_meets_the_deadline_whenever_the_prices_arrive(
    amps: int, phases: int, arrival: str | None
) -> None:
    """A sweep over needs and arrival times: the deadline always holds.

    When the prices never come the car draws only 90 percent of its power (the margin the guarantee
    is sized for). When they do come, an ordinary plan follows, and an ordinary plan is price-optimal
    with no derating of its own -- exactly as it always was -- so the car draws everything it is asked.
    """
    when = None
    if arrival is not None:
        clock, _, plus = arrival.partition("+")
        day = "2026-09-23" if plus else "2026-09-22"
        when = at(f"{day}T{clock.rjust(5, '0')}:00+02:00")
    kw = power_kw(amps, phases)
    for need in (8.0, 25.0, 60.0):
        if need > 23 * kw * 0.6:
            continue  # too tight for an ordinary plan at 90 percent draw: not this module's promise
        outcome, deadline = run_day(
            start=MORNING, deadline_local=EIGHT, need_kwh=need, amps=amps, phases=phases,
            prices_arrive_at=when, today=TODAY, tomorrow=TOMORROW,
            actual_rate=0.9 if when is None else 1.0,
        )
        assert outcome.delivered_kwh >= need - 1e-9, (need, arrival)
        assert outcome.finished_at is not None and outcome.finished_at <= deadline, (need, arrival)
        after_hours = (deadline - max(when, at("2026-09-22T13:45:00+02:00"))).total_seconds() / 3600 if when else 0
        if when is not None and need <= after_hours * kw * 0.8:
            assert outcome.unpriced_slots == 0, "it could wait, so only real prices are ever used"
