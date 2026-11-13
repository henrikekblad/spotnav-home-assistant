"""Per-hour power caps in the planner (`PlanRequest.power_cap_kw_by_hour`): the house load forecast's hours.

Pure arithmetic. Without per-hour caps, or with caps the same in every hour, the plan is exactly the one without them;
with caps that differ, the slots are chosen by energy (`_choose_by_energy`), whose start cost and shortest block are
checked against a brute force.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.planning.planner import (
    AUTO_PERIODS,
    ENERGY_SEARCH_LIMIT,
    SHORTEST_BLOCK_SLOTS,
    SLOT_HOURS,
    FiscalChoice,
    PlanRequest,
    PlannerInputError,
    PlanningSlot,
    _choose_by_energy,
    calculate_plan,
    cheapest_slots,
    ending_at_need,
    energy_per_slot_kwh,
    plan_unpriced,
    power_kw,
    start_cost_minor,
)

from .planning import parse_documents

TZ = "Europe/Stockholm"
ZONE = ZoneInfo(TZ)
#: A one-phase 16 A car: 3.68 kW. Its minimum (6 A) is 1.38 kW.
CAR_KW = power_kw(16, 1)


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=ZONE)


def hours_of(day: int, first: int, last: int) -> list[datetime]:
    """The local hours `first` up to `last` (not included) of a day, as UTC hour starts."""
    return [local(day, hour).astimezone(timezone.utc) for hour in range(first, last)]


def documents(day12: list[float], day13: list[float]) -> tuple:
    return parse_documents(
        [
            {"area": "SE4", "date": f"2026-09-{day}", "tz": TZ, "res": 60,
             "start": f"2026-09-{day}T00:00:00+02:00", "unit": "EUR/kWh", "prices": prices}
            for day, prices in ((12, day12), (13, day13))
        ]
    )


def evening_and_night_prices() -> tuple:
    """Cheap at 17-20 (0.50) and at 01-05 (0.55), dear otherwise."""
    day12 = [2.0] * 24
    for hour in (17, 18, 19):
        day12[hour] = 0.50
    day13 = [2.0] * 24
    for hour in (1, 2, 3, 4):
        day13[hour] = 0.55
    return documents(day12, day13)


def request(**changes) -> PlanRequest:
    base = PlanRequest(
        area_id="SE4",
        timezone=TZ,
        currency="EUR",
        major_unit="EUR",
        minor_unit="cent",
        documents=evening_and_night_prices(),
        now=local(12, 12),
        phases=1,
        amps=16,
        requested_kwh=7.0,
        consumption_kwh_per_10km=1.5,
        max_periods=AUTO_PERIODS,
        departure=time(7, 0),
    )
    return replace(base, **changes)


def evening_peak_caps(flat_kw: float) -> dict[datetime, float]:
    """A house at 4 kW in the evening (the target leaves the car's minimum) and 0.5 kW at night (the car's own power
    and more), 2 kW otherwise (the flat figure)."""
    caps = {start: flat_kw for start in hours_of(12, 12, 24) + hours_of(13, 0, 12)}
    for start in hours_of(12, 17, 20):
        caps[start] = 1.38
    for start in hours_of(12, 22, 24) + hours_of(13, 0, 7):
        caps[start] = 4.4
    return caps


def chosen_local_hours(result) -> set[int]:
    return {slot.start.astimezone(ZONE).hour for slot in result.slots}


# ------------------------------------------------------------------------------------------------ no forecast


def test_without_per_hour_caps_the_plan_is_the_flat_one() -> None:
    flat = calculate_plan(request(power_cap_kw=2.9))
    assert calculate_plan(request(power_cap_kw=2.9, power_cap_kw_by_hour=None)) == flat
    # An empty forecast is no forecast.
    assert calculate_plan(request(power_cap_kw=2.9, power_cap_kw_by_hour={})) == flat
    assert flat.slot_kwh == ()


def test_a_flat_cap_in_every_hour_is_exactly_the_plan_with_the_flat_figure() -> None:
    """Equal energy per slot: the plan is the count search's, to the last bit."""
    flat = calculate_plan(request(power_cap_kw=2.9))
    same = {start: 2.9 for start in hours_of(12, 0, 24) + hours_of(13, 0, 24)}
    assert calculate_plan(request(power_cap_kw=2.9, power_cap_kw_by_hour=same)) == flat
    # Caps above the car's own power in every hour change nothing either, whatever they are.
    above = {start: 5.0 + index for index, start in enumerate(same)}
    assert calculate_plan(request(power_cap_kw_by_hour=above)) == calculate_plan(request())


def test_hours_the_forecast_does_not_name_fall_back_to_the_flat_cap() -> None:
    """Only a few hours named, all at the flat figure: still the flat plan."""
    flat = calculate_plan(request(power_cap_kw=2.9))
    partial = {start: 2.9 for start in hours_of(12, 17, 20)}
    assert calculate_plan(request(power_cap_kw=2.9, power_cap_kw_by_hour=partial)) == flat


@pytest.mark.parametrize("periods", [1, 2, 3, AUTO_PERIODS])
def test_flat_caps_keep_the_equal_energy_invariants(periods) -> None:
    flat = calculate_plan(request(power_cap_kw=2.9, max_periods=periods))
    assert flat.has_plan
    per_slot = energy_per_slot_kwh(16, 1, cap_kw=2.9)
    assert flat.delivered_kwh == pytest.approx(flat.slots_needed * per_slot)
    assert flat.slots_needed == len(flat.slots)
    assert flat.power_kw == pytest.approx(2.9)


# ------------------------------------------------------------------------------------------------ an evening peak


def test_an_evening_peak_picks_other_hours_than_the_flat_figure() -> None:
    """The flat figure counts on 2.9 kW in the cheap evening and buys it alone; the forecast knows the evening leaves
    the car only its minimum, so the plan adds night hours to cover the need."""
    flat = calculate_plan(request(power_cap_kw=2.9))
    hourly = calculate_plan(request(power_cap_kw=2.9, power_cap_kw_by_hour=evening_peak_caps(2.9)))
    assert flat.has_plan and hourly.has_plan
    assert chosen_local_hours(flat) <= {17, 18, 19}
    assert chosen_local_hours(hourly) != chosen_local_hours(flat)
    assert chosen_local_hours(hourly) & {1, 2, 3, 4}
    assert hourly.delivered_kwh >= 7.0
    # What each slot delivers is its hour's cap for a quarter-hour; the stated power is their mean.
    for slot, kwh in zip(hourly.slots, hourly.slot_kwh, strict=True):
        hour = slot.start.astimezone(ZONE).hour
        assert kwh == pytest.approx((1.38 if hour in (17, 18, 19) else min(4.4, CAR_KW)) * SLOT_HOURS)
    assert hourly.delivered_kwh == pytest.approx(sum(hourly.slot_kwh))
    assert hourly.power_kw == pytest.approx(hourly.delivered_kwh / (len(hourly.slots) * SLOT_HOURS))
    assert hourly.slots_needed == hourly.priced_slots == len(hourly.slots)
    expected_cost = sum(
        kwh * slot.effective_minor_per_kwh for slot, kwh in zip(hourly.slots, hourly.slot_kwh, strict=True)
    ) / 100
    assert hourly.estimated_cost == pytest.approx(expected_cost)


def test_a_slot_is_added_only_while_the_need_is_not_covered() -> None:
    hourly = calculate_plan(request(power_cap_kw=2.9, power_cap_kw_by_hour=evening_peak_caps(2.9)))
    # Without its last slot the plan would not cover the need: no slot is bought beyond it.
    assert hourly.delivered_kwh - hourly.slot_kwh[-1] < 7.0


def test_the_evening_at_the_minimum_is_not_bought_when_fewer_night_slots_cost_less() -> None:
    """With a start cost per run, the whole need at night (two cheap hours at the car's own power) costs less than the
    evening at the minimum plus the night's remainder: the plan leaves the evening."""
    hourly = calculate_plan(request(power_cap_kw=2.9, power_cap_kw_by_hour=evening_peak_caps(2.9)))
    assert chosen_local_hours(hourly) <= {1, 2, 3, 4}
    assert len(hourly.periods) == 1


def test_cheapest_energy_stays_the_rule_with_per_hour_caps() -> None:
    """Without a start cost (two periods) and a need the night's two hours cannot cover in whole slots without a
    third hour, the evening's cheaper energy at the minimum is bought, and the night covers the rest."""
    hourly = calculate_plan(
        request(requested_kwh=7.5, max_periods=2, power_cap_kw=2.9, power_cap_kw_by_hour=evening_peak_caps(2.9))
    )
    assert chosen_local_hours(hourly) & {17, 18, 19}
    assert chosen_local_hours(hourly) & {1, 2, 3, 4}
    assert hourly.delivered_kwh >= 7.5


def test_the_window_power_is_the_mean_of_its_slots() -> None:
    hourly = calculate_plan(
        request(requested_kwh=7.5, max_periods=2, power_cap_kw=2.9, power_cap_kw_by_hour=evening_peak_caps(2.9))
    )
    evening = hourly.window_power_kw(local(12, 17), local(12, 20))
    night = hourly.window_power_kw(local(13, 0), local(13, 7))
    assert evening == pytest.approx(1.38)
    assert night == pytest.approx(min(4.4, CAR_KW))
    # Outside every slot, and for a flat plan, the plan's own power.
    assert hourly.window_power_kw(local(12, 12), local(12, 13)) == hourly.power_kw
    flat = calculate_plan(request(power_cap_kw=2.9))
    assert flat.window_power_kw(local(12, 17), local(12, 20)) == flat.power_kw


def test_a_plan_that_stops_at_its_need_counts_its_last_slot_at_that_slot_s_own_power() -> None:
    hourly = calculate_plan(request(power_cap_kw=2.9, power_cap_kw_by_hour=evening_peak_caps(2.9)))
    stopped = ending_at_need(hourly)
    assert stopped.delivered_kwh == pytest.approx(7.0)
    extra = hourly.delivered_kwh - 7.0
    last = hourly.slots[-1]
    assert stopped.estimated_cost == pytest.approx(hourly.estimated_cost - extra * last.effective_minor_per_kwh / 100)
    met = stopped.need_met_at
    assert met is not None
    last_kw = hourly.slot_kwh[-1] / SLOT_HOURS
    assert (hourly.periods[-1][1] - met).total_seconds() == pytest.approx(extra / last_kw * 3600)
    # Charging now, stopped at the need, is never dearer than charging now in whole slots.
    assert stopped.now_cost is not None and hourly.now_cost is not None
    assert stopped.now_cost <= hourly.now_cost + 1e-9


def test_a_departure_too_close_takes_every_slot_whatever_each_delivers() -> None:
    """Best effort: 17:00-19:00 at the minimum cannot deliver 7 kWh; every slot up to the departure is bought."""
    short = calculate_plan(
        request(now=local(12, 17), departure=time(19, 0), power_cap_kw=2.9, power_cap_kw_by_hour=evening_peak_caps(2.9))
    )
    assert short.has_plan and short.short_of_deadline
    assert len(short.slots) == 8
    assert short.delivered_kwh == pytest.approx(8 * 1.38 * SLOT_HOURS)
    # The flat figure would have said it fits (2.9 kW x 2 h = 5.8 kWh < 7 is short too, but by less).
    flat = calculate_plan(request(now=local(12, 17), departure=time(19, 0), power_cap_kw=2.9))
    assert flat.short_of_deadline and flat.delivered_kwh > short.delivered_kwh


def test_a_need_that_fits_only_at_the_forecast_s_night_power_is_not_short() -> None:
    """The flat figure says 7 kWh does not fit in 01:00-03:00 (2.9 kW x 2 h); the night's forecast says it does."""
    caps = {start: 4.4 for start in hours_of(13, 0, 4)}
    req = request(now=local(13, 1), departure=time(3, 0), requested_kwh=7.0, power_cap_kw=2.9)
    assert calculate_plan(req).short_of_deadline
    hourly = calculate_plan(replace(req, power_cap_kw_by_hour=caps))
    assert hourly.has_plan and not hourly.short_of_deadline
    assert hourly.delivered_kwh >= 7.0


def test_unpriced_charging_counts_each_slot_at_its_hour() -> None:
    unpriced = plan_unpriced(request(power_cap_kw=2.9, power_cap_kw_by_hour=evening_peak_caps(2.9), now=local(12, 17)))
    assert unpriced.has_plan
    assert unpriced.delivered_kwh >= 7.0
    assert unpriced.delivered_kwh == pytest.approx(sum(unpriced.slot_kwh))
    assert unpriced.delivered_kwh - unpriced.slot_kwh[-1] < 7.0


def test_a_search_too_large_plans_with_the_flat_figure() -> None:
    caps = {start: 1.0 + (index % 3) * 0.5 for index, start in enumerate(hours_of(12, 12, 24) + hours_of(13, 0, 24))}
    big = request(requested_kwh=20.0, max_periods=8, departure=None, power_cap_kw=2.9, power_cap_kw_by_hour=caps)
    flat = calculate_plan(replace(big, power_cap_kw_by_hour=None))
    # (20 kWh in steps of a quarter of 0.25 kWh) x (8 runs + 1) x 2 x 96 candidates is above the limit.
    assert 321 * 18 * 96 > ENERGY_SEARCH_LIMIT
    assert flat.has_plan
    assert calculate_plan(big) == flat


@pytest.mark.parametrize(
    ("caps", "code"),
    [
        ({datetime(2026, 9, 12, 17): 2.0}, "invalid_timestamp"),
        ({local(12, 17): 0.0}, "invalid_number"),
        ({local(12, 17): float("nan")}, "invalid_number"),
    ],
)
def test_bad_per_hour_caps_are_refused(caps, code) -> None:
    with pytest.raises(PlannerInputError) as caught:
        request(power_cap_kw_by_hour=caps).validated()
    assert caught.value.code == code


def test_hour_keys_in_any_zone_name_the_same_hour() -> None:
    utc = calculate_plan(request(power_cap_kw=2.9, power_cap_kw_by_hour=evening_peak_caps(2.9)))
    in_local = {start.astimezone(ZONE): cap for start, cap in evening_peak_caps(2.9).items()}
    assert calculate_plan(request(power_cap_kw=2.9, power_cap_kw_by_hour=in_local)) == utc


# ------------------------------------------------------------------------------------------------ the energy search

START = datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc)


def slots_of(prices: list[float]) -> list[PlanningSlot]:
    return [PlanningSlot(START + timedelta(minutes=15 * index), price, "d") for index, price in enumerate(prices)]


def runs_of(chosen: tuple[int, ...]) -> list[int]:
    lengths: list[int] = []
    for position, index in enumerate(chosen):
        if position and index == chosen[position - 1] + 1:
            lengths[-1] += 1
        else:
            lengths.append(1)
    return lengths


def brute_force(slots, energies, need, cap, fiscal) -> float | None:
    """The lowest objective over every subset that covers the need, adds no slot once covered (the last slot is the
    one that covers it) and keeps the rules on runs."""
    auto = cap is AUTO_PERIODS
    start_cost = start_cost_minor(slots, fiscal) if auto else 0.0
    prices = [slot.local_major_per_kwh * 100 for slot in slots]
    shortest = 1 if max(energies) >= need else SHORTEST_BLOCK_SLOTS
    best = None
    for size in range(1, len(slots) + 1):
        for chosen in itertools.combinations(range(len(slots)), size):
            covered = sum(energies[index] for index in chosen)
            if covered < need - 1e-9 or covered - energies[chosen[-1]] >= need - 1e-9:
                continue
            runs = runs_of(chosen)
            if auto and any(length < shortest for length in runs):
                continue
            if not auto and len(runs) > cap:
                continue
            value = sum(energies[index] * prices[index] for index in chosen) + start_cost * len(runs)
            best = value if best is None or value < best else best
    return best


def objective(slots, energies, chosen, cap, fiscal) -> float:
    auto = cap is AUTO_PERIODS
    start_cost = start_cost_minor(slots, fiscal) if auto else 0.0
    return sum(energies[index] * slots[index].local_major_per_kwh * 100 for index in chosen) + start_cost * len(
        runs_of(chosen)
    )


@pytest.mark.parametrize("seed", range(40))
def test_the_energy_search_finds_the_brute_force_optimum(seed: int) -> None:
    """Energies on the search's own steps (quarters of the smallest slot), so its merging loses nothing."""
    rng = random.Random(seed)
    count = rng.randint(4, 9)
    slots = slots_of([round(rng.uniform(0.2, 2.0), 2) for _ in range(count)])
    energies = [rng.choice((0.5, 0.75, 1.0, 1.25)) for _ in range(count)]
    need = rng.choice((0.9, 1.5, 2.2, 3.0))
    cap = rng.choice((1, 2, AUTO_PERIODS))
    fiscal = FiscalChoice()
    found = _choose_by_energy(slots, energies, need, cap, fiscal, None)
    expected = brute_force(slots, energies, need, cap, fiscal)
    if expected is None:
        assert found is None
        return
    assert found is not None
    assert sum(energies[index] for index in found) >= need - 1e-9
    assert objective(slots, energies, found, cap, fiscal) == pytest.approx(expected)
    if cap is AUTO_PERIODS:
        shortest = 1 if max(energies) >= need else SHORTEST_BLOCK_SLOTS
        assert min(runs_of(found)) >= shortest
    else:
        assert len(runs_of(found)) <= cap


@pytest.mark.parametrize("seed", range(30))
@pytest.mark.parametrize("cap", [1, 2, 3, AUTO_PERIODS])
def test_equal_energies_choose_what_the_count_search_chooses(seed: int, cap) -> None:
    """The energy search with every slot alike is the count search: the same slots, ties to the latest included."""
    rng = random.Random(seed)
    count = rng.randint(6, 30)
    slots = slots_of([rng.choice((0.5, 0.6, 0.7, 1.0)) for _ in range(count)])
    per_slot = energy_per_slot_kwh(16, 3)
    needed = rng.randint(1, max(1, count // 2))
    need = per_slot * (needed - 0.5)
    by_count = cheapest_slots(slots, needed, per_slot, cap, FiscalChoice(), None)
    found = _choose_by_energy(slots, [per_slot] * count, need, cap, FiscalChoice(), None)
    assert (None if by_count is None else tuple(slots.index(slot) for slot in by_count)) == found


def test_one_slot_that_covers_the_need_may_be_a_run_of_its_own() -> None:
    """The shortest block is half an hour unless one slot covers the need, as with a count of one."""
    slots = slots_of([2.0, 0.5, 2.0, 2.0])
    found = _choose_by_energy(slots, [0.5, 1.0, 0.5, 0.5], 0.8, AUTO_PERIODS, FiscalChoice(), None)
    assert found == (1,)
    # A need no slot covers alone keeps every run at least two slots long.
    found = _choose_by_energy(slots, [0.5, 1.0, 0.5, 0.5], 1.2, AUTO_PERIODS, FiscalChoice(), None)
    assert found is not None and min(runs_of(found)) >= SHORTEST_BLOCK_SLOTS


def test_the_start_cost_bridges_a_low_power_gap_as_it_bridges_a_dear_one() -> None:
    """A slot at a lower power in the middle of a cheap run costs less to bridge than a second start."""
    slots = slots_of([2.0, 1.0, 1.0, 1.0, 1.0, 1.0, 2.0])
    energies = [1.0, 1.0, 1.0, 0.75, 1.0, 1.0, 1.0]
    found = _choose_by_energy(slots, energies, 4.5, AUTO_PERIODS, FiscalChoice(), None)
    assert found == (1, 2, 3, 4, 5)
    assert runs_of(found) == [5]


def test_the_deadline_bounds_the_energy_search() -> None:
    slots = slots_of([0.1, 0.1, 2.0, 2.0, 2.0, 0.05, 0.05])
    energies = [0.5, 0.5, 1.0, 1.0, 1.0, 1.0, 1.0]
    found = _choose_by_energy(slots, energies, 1.5, 2, FiscalChoice(), START + timedelta(minutes=75))
    assert found is not None and max(found) <= 4
