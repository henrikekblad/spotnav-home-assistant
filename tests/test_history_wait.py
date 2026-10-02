"""History wait: weighing the cheapest published placement against what history expects of the unknown hours.

Pure: a profile, a window and a plan go in, a decision comes out. The closed-loop behaviour is in
`tests/test_dated_departure.py`; here are the numbers: the margin rule, the conversion to the user's
effective price, the quarters a profile can and cannot price, and the clock-change weekends.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.planning import history_wait
from custom_components.spotnav.planning.planner import (
    calculate_plan,
    FiscalChoice,
    PlanRequest,
    price_gap,
    PriceGap,
)
from custom_components.spotnav.pricing.relay_contract import parse_day, parse_profile

from .relay import flat_day, profile_document

STOCKHOLM = ZoneInfo("Europe/Stockholm")
#: Thursday 2026-10-01, 20:00 local.
NOW = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
SUNDAY = date(2026, 10, 4)
RATE = 11.275  # SEK per EUR, the day document's own


def documents(*days: date, price: float = 0.10) -> tuple:
    return tuple(
        parse_day(json.loads(flat_day("SE4", day, price=price)), area_id="SE4", day=day) for day in days
    )


def request(
    *,
    kwh: float = 10.0,
    fiscal: FiscalChoice | None = None,
    when: date = SUNDAY,
    departure: time = time(8, 0),
    published: tuple[date, ...] = (date(2026, 10, 1), date(2026, 10, 2)),
    price: float = 0.10,
    max_periods: int = 1,
) -> PlanRequest:
    return PlanRequest(
        area_id="SE4",
        timezone="Europe/Stockholm",
        currency="SEK",
        major_unit="kr",
        minor_unit="öre",
        documents=documents(*published, price=price),
        now=NOW,
        phases=1,
        amps=10,
        requested_kwh=kwh,
        consumption_kwh_per_10km=2.0,
        fiscal=fiscal or FiscalChoice(),
        max_periods=max_periods,
        departure=departure,
        departure_date=when,
    ).validated()


def decide(
    base: PlanRequest, profile_document_: dict | None
) -> tuple[history_wait.HistoryDecision, PriceGap]:
    gap = price_gap(base)
    assert gap is not None
    known = calculate_plan(_with_window(base, gap))
    assert known.has_plan, known.reason
    profile = None if profile_document_ is None else parse_profile(profile_document_, area_id="SE4")
    return history_wait.evaluate(base, gap, known, profile), gap


def _with_window(base: PlanRequest, gap: PriceGap) -> PlanRequest:
    return replace(base, window_end=gap.missing_from)


SUNDAY_NIGHT = {(7, hour): 0.03 for hour in range(8)}


def test_the_window_of_a_dated_departure_reaches_its_deadline_and_the_gap_is_what_is_unpublished() -> None:
    base = request()
    gap = price_gap(base)
    assert gap is not None
    assert gap.missing_from == datetime(2026, 10, 3, 0, 0, tzinfo=STOCKHOLM)
    assert gap.deadline == datetime(2026, 10, 4, 8, 0, tzinfo=STOCKHOLM)
    assert gap.first_start == datetime(2026, 10, 1, 20, 0, tzinfo=STOCKHOLM)


def test_a_clear_saving_beyond_one_standard_deviation_argues_for_waiting() -> None:
    decision, _ = decide(request(), profile_document(cheap=SUNDAY_NIGHT, std=0.01))

    assert decision.outcome == "wait"
    # 10 kWh at 2.3 kW is 11 quarters; the cheapest placement is in Sunday's 0.03 EUR hours.
    assert decision.known_mean_minor == pytest.approx(0.10 * RATE * 100)
    assert decision.expected_mean_minor == pytest.approx(0.03 * RATE * 100)
    assert decision.margin_minor == pytest.approx(0.01 * RATE * 100)
    assert decision.weekday == 7 and decision.weeks == 4
    assert decision.percent == 70
    assert decision.unknown_slots == 32 * 4


def test_the_saving_must_exceed_the_margin_not_merely_exist() -> None:
    at_the_margin, _ = decide(request(), profile_document(cheap={(7, h): 0.09 for h in range(8)}, std=0.01))
    assert at_the_margin.outcome == "plan_known"  # equal to the margin is not beyond it
    just_beyond, _ = decide(request(), profile_document(cheap={(7, h): 0.0899 for h in range(8)}, std=0.01))
    assert just_beyond.outcome == "wait"
    noisy, _ = decide(request(), profile_document(cheap=SUNDAY_NIGHT, std=0.08))
    assert noisy.outcome == "plan_known", "a spread as wide as the saving is no argument"
    assert noisy.percent == 70


def test_a_flat_history_or_a_dearer_weekend_plans_on_the_published_prices() -> None:
    flat, _ = decide(request(), profile_document())
    assert flat.outcome == "plan_known" and flat.percent == 0
    dearer, _ = decide(request(), profile_document(cheap={(7, h): 0.30 for h in range(24)}))
    assert dearer.outcome == "plan_known" and dearer.percent == 0


def test_the_median_is_converted_exactly_like_a_published_price() -> None:
    fiscal = FiscalChoice(
        tax_enabled=True, tax_minor_per_kwh=36.0, transfer_enabled=True, transfer_minor_per_kwh=25.0,
        vat_enabled=True, vat_percent=25.0,
    )
    decision, _ = decide(request(fiscal=fiscal), profile_document(cheap=SUNDAY_NIGHT, std=0.01))

    def effective(eur: float) -> float:
        return (eur * RATE * 100 + 36.0 + 25.0) * 1.25

    assert decision.known_mean_minor == pytest.approx(effective(0.10))
    assert decision.expected_mean_minor == pytest.approx(effective(0.03))
    # The spread passes through the same conversion: tax and fee are added (no spread), VAT scales it.
    assert decision.margin_minor == pytest.approx(0.01 * RATE * 100 * 1.25)


def test_an_hour_the_profile_omits_is_never_claimed_cheap() -> None:
    omitted = {(7, hour) for hour in range(8)}
    decision, _ = decide(
        request(), profile_document(cheap={(7, 8): 0.01}, omit=omitted, median=0.10)
    )
    assert decision.outcome == "plan_known"
    # Nothing before 08:00 on Sunday is priced; the quarters that are (the rest of the window) are not cheaper.
    assert decision.unknown_slots == (32 - 8) * 4


def test_without_enough_priced_hours_for_the_whole_need_there_is_no_basis() -> None:
    only_one_hour = {(7, hour) for hour in range(24)} - {(7, 3)}
    everything_else = {(w, h) for w in range(1, 8) for h in range(24)} - {(7, 3)}
    decision, _ = decide(request(kwh=10.0), profile_document(omit=everything_else, cheap={(7, 3): 0.01}))
    assert decision.outcome == "no_profile" and decision.unknown_slots == 4
    assert only_one_hour  # the helper set above is only there to name the case


def test_no_profile_is_no_basis_and_says_so() -> None:
    decision, _ = decide(request(), None)
    assert decision == history_wait.HistoryDecision("no_profile")


def test_the_period_cap_binds_the_expected_placement_like_a_real_one() -> None:
    # Two cheap hours far apart; one run cannot use both, so the expected price is dearer than with two runs.
    cheap = {(6, 3): 0.01, (7, 5): 0.01}
    one_run, _ = decide(request(kwh=4.6, max_periods=1), profile_document(cheap=cheap))
    two_runs, _ = decide(request(kwh=4.6, max_periods=2), profile_document(cheap=cheap))
    assert one_run.expected_mean_minor > two_runs.expected_mean_minor


def test_the_unsafe_flag_keeps_every_number() -> None:
    decision, _ = decide(request(), profile_document(cheap=SUNDAY_NIGHT))
    unsafe = history_wait.unsafe(decision)
    assert unsafe.outcome == "unsafe_to_wait"
    assert unsafe.known_mean_minor == decision.known_mean_minor and unsafe.weekday == 7
    assert set(unsafe.as_diagnostics()) == {
        "outcome", "known_mean_minor", "expected_mean_minor", "margin_minor",
        "weekday", "percent", "weeks", "unknown_slots",
    }


# -------------------------------------------------------------------------------- clock changes


def gap(missing_from: datetime, deadline: datetime) -> PriceGap:
    return PriceGap(first_start=missing_from - timedelta(hours=4), missing_from=missing_from, deadline=deadline, known=())


def test_the_autumn_weekend_repeats_an_hour_and_every_elapsed_quarter_is_priced_once() -> None:
    profile = parse_profile(profile_document(), area_id="SE4")
    window = gap(
        datetime(2026, 10, 24, 0, 0, tzinfo=STOCKHOLM), datetime(2026, 10, 25, 8, 0, tzinfo=STOCKHOLM)
    )
    slots = history_wait.unknown_slots(window, profile, rate=RATE)

    # 24 h of Saturday + 8 h (CEST 0-3) + the repeated hour + CET 3-8: 33 elapsed hours, not 32.
    assert len(slots) == 33 * 4
    assert len({slot.start for slot, _ in slots}) == len(slots)
    sunday_two = [
        slot for slot, _ in slots if slot.start.astimezone(STOCKHOLM).hour == 2
        and slot.start.astimezone(STOCKHOLM).day == 25
    ]
    assert len(sunday_two) == 8, "02:00-02:59 happens twice on the 25-hour day"
    assert slots[-1][0].end == datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)


def test_the_spring_weekend_skips_an_hour_and_never_prices_the_one_that_does_not_exist() -> None:
    profile = parse_profile(profile_document(), area_id="SE4")
    window = gap(
        datetime(2026, 3, 28, 0, 0, tzinfo=STOCKHOLM), datetime(2026, 3, 29, 8, 0, tzinfo=STOCKHOLM)
    )
    slots = history_wait.unknown_slots(window, profile, rate=RATE)

    assert len(slots) == 31 * 4
    local_hours = {slot.start.astimezone(STOCKHOLM).hour for slot, _ in slots if slot.start.astimezone(STOCKHOLM).day == 29}
    assert 2 not in local_hours and {0, 1, 3, 4, 5, 6, 7} == local_hours


def test_the_deadline_instant_of_a_dated_departure_is_the_wall_time_on_a_clock_change_day() -> None:
    from custom_components.spotnav.planning.planner import _window

    base = request(when=date(2026, 10, 25), published=(date(2026, 10, 24), date(2026, 10, 25)))
    # Published days are 24-hour documents here, so only the deadline is asked of the window.
    window = _window(replace(base, now=datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)))
    assert not isinstance(window, str)
    assert window.deadline == datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)
    assert window.deadline.astimezone(STOCKHOLM).hour == 8

    spring = _window(
        replace(
            request(when=date(2026, 3, 29), published=(date(2026, 3, 27), date(2026, 3, 28))),
            now=datetime(2026, 3, 27, 18, 0, tzinfo=timezone.utc),
        )
    )
    assert not isinstance(spring, str)
    assert spring.deadline == datetime(2026, 3, 29, 6, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------------- the planner itself


def test_a_date_needs_a_departure_time_and_is_a_calendar_date() -> None:
    from custom_components.spotnav.planning.planner import PlannerInputError

    base = request()
    with pytest.raises(PlannerInputError) as error:
        replace(base, departure=None).validated()
    assert error.value.code == "invalid_departure"
    with pytest.raises(PlannerInputError) as error:
        replace(base, departure_date=datetime(2026, 10, 4, 8, 0)).validated()
    assert error.value.code == "invalid_departure"


def test_a_dated_departure_plans_past_the_24_hours_of_a_daily_one() -> None:
    from .relay import cheap_night_day

    days = (date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3), SUNDAY)
    docs = tuple(
        parse_day(
            json.loads(cheap_night_day("SE4", day) if day == SUNDAY else flat_day("SE4", day, price=0.10)),
            area_id="SE4",
            day=day,
        )
        for day in days
    )
    dated = replace(request(), documents=docs)
    plan = calculate_plan(dated)

    assert plan.has_plan and plan.unpriced is False
    # The cheapest of the whole window is Sunday's cheap night, three days after the first slot.
    assert plan.slots[0].start >= datetime(2026, 10, 4, 0, 0, tzinfo=STOCKHOLM)
    assert plan.slots[-1].end <= datetime(2026, 10, 4, 8, 0, tzinfo=STOCKHOLM)
    # The same request as a daily departure only sees the next 24 hours.
    daily = calculate_plan(replace(dated, departure_date=None))
    assert daily.slots[-1].end <= datetime(2026, 10, 2, 8, 0, tzinfo=STOCKHOLM)


def test_a_window_end_asks_for_no_price_past_itself() -> None:
    base = request()
    gap = price_gap(base)
    assert gap is not None
    known = calculate_plan(replace(base, window_end=gap.missing_from))
    assert known.has_plan
    assert known.slots[-1].end <= gap.missing_from
    # Without a tighter window the same request needs the unpublished weekend and is refused as such.
    assert calculate_plan(base).reason == "insufficient_price_horizon"
