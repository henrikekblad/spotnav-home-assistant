"""History wait: is it worth leaving the unpublished hours of a dated departure for later?

A dated departure ("Sunday 08:00") reaches past the last published price. What the market will do in
those unknown hours is not known, but what it did on the same weekdays and hours over the last weeks is
(the relay's profile, `pricing/relay_contract.PriceProfile`). This module weighs the two, and only
decides whether history *argues for waiting*; whether waiting is safe is `planning/price_wait.py`'s
capacity rule and stays there.

    known_mean    = mean effective price of the cheapest placement of the whole need in published intervals
    expected_mean = mean expected effective price of the cheapest placement in the unknown hours
    margin        = k x the profile's spread (k = 1) for those hours, as an effective price

    wait when known_mean - expected_mean > margin

Nothing here plans on a guess: a profile only ever argues for leaving the charge for later, and the
published intervals decide what is actually charged. The profile's EUR median is converted exactly as a
day price is (the latest day's own rate, then tax, transfer fee and VAT, `planner.effective_minor_per_kwh`);
the spread is converted the same way, so only the VAT factor scales it.

Pure: no Home Assistant, no clock, no storage.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import timedelta, timezone
from typing import Any, Final, Literal

from homeassistant.util import dt as dt_util

from ..pricing.relay_contract import PriceProfile
from .planner import (
    cheapest_slots,
    document_rate,
    effective_minor_per_kwh,
    energy_per_slot_kwh,
    FiscalChoice,
    PlannerInputError,
    PlanningSlot,
    PlanRequest,
    PlanResult,
    PriceGap,
    STEP_MINUTES,
)


#: How many standard deviations the expected saving must exceed (decided: one).
MARGIN_STANDARD_DEVIATIONS: Final = 1.0

#: A saving equal to the margin is not beyond it; this much float noise (minor units per kWh) cannot make it so.
EPSILON_MINOR: Final = 1e-9

Outcome = Literal[
    #: History argues for leaving the charge for the unpublished hours (safety is judged separately).
    "wait",
    #: History shows no clear saving: plan on what is published.
    "plan_known",
    #: There is no usable profile, or no hour of it covers the unknown window.
    "no_profile",
    #: History argues for waiting but the deadline would not be safe: plan on what is published.
    "unsafe_to_wait",
]


@dataclass(frozen=True, slots=True)
class HistoryDecision:
    """What the history said, with every number behind it (for the status line and diagnostics)."""

    outcome: Outcome
    #: Effective prices in the minor unit per kWh; `None` when no comparison could be made.
    known_mean_minor: float | None = None
    expected_mean_minor: float | None = None
    margin_minor: float | None = None
    #: The weekday (ISO 1..7) most of the expected cheapest hours fall on, the saving in whole percent of
    #: the known mean, and the weeks of history behind it.
    weekday: int | None = None
    percent: int | None = None
    weeks: int | None = None
    #: How many unknown quarter-hours the profile priced.
    unknown_slots: int = 0
    #: The published placement is cheaper than the expected unpublished one by more than the margin
    #: (a daily departure does not wait then). Informational for a dated departure.
    known_cheaper: bool = False

    def as_diagnostics(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "known_mean_minor": self.known_mean_minor,
            "expected_mean_minor": self.expected_mean_minor,
            "margin_minor": self.margin_minor,
            "weekday": self.weekday,
            "percent": self.percent,
            "weeks": self.weeks,
            "unknown_slots": self.unknown_slots,
            "known_cheaper": self.known_cheaper,
        }


def unknown_slots(
    gap: PriceGap, profile: PriceProfile, *, rate: float
) -> tuple[tuple[PlanningSlot, float], ...]:
    """Every unpublished quarter-hour before the deadline that the profile has an hour for.

    Each slot's `local_major_per_kwh` is the profile's median in the area's currency; the number beside
    it is the spread, in the same unit. Stepped in absolute time and read in the profile's zone, so a clock
    change repeats or skips an hour the way the day files do. A quarter whose weekday-hour the profile
    omitted (too few samples) is left out: no history, no claim.
    """
    zone = dt_util.get_time_zone(profile.tz)
    step = timedelta(minutes=STEP_MINUTES)
    # Absolute time throughout: wall-clock arithmetic on a zone-aware value would repeat or skip an hour wrongly.
    cursor = gap.missing_from.astimezone(timezone.utc)
    deadline = gap.deadline.astimezone(timezone.utc)
    out: list[tuple[PlanningSlot, float]] = []
    while cursor + step <= deadline:
        local = cursor.astimezone(zone)
        entry = profile.hour(local.isoweekday(), local.hour)
        if entry is not None:
            out.append(
                (PlanningSlot(start=cursor, local_major_per_kwh=entry.median * rate, source="history"), entry.std * rate)
            )
        cursor += step
    return tuple(out)


def evaluate(
    request: PlanRequest,
    gap: PriceGap,
    known: PlanResult,
    profile: PriceProfile | None,
) -> HistoryDecision:
    """Weigh the cheapest published placement (`known`) against what history expects of the unknown hours.

    `request` is the validated dated request, `gap` its price gap and `known` the plan in published
    intervals (with a plan). Returns `wait` only when the saving exceeds the margin; the caller then asks
    `price_wait.decide` whether waiting is safe.
    """
    if profile is None or not known.has_plan or not request.documents:
        return HistoryDecision("no_profile")
    latest = max(request.documents, key=lambda document: document.day)
    try:
        rate = document_rate(latest, request.currency)
    except PlannerInputError:
        return HistoryDecision("no_profile", weeks=profile.weeks)
    candidates = unknown_slots(gap, profile, rate=rate)
    needed = known.slots_needed
    if len(candidates) < needed:
        return HistoryDecision("no_profile", weeks=profile.weeks, unknown_slots=len(candidates))
    slots = [slot for slot, _ in candidates]
    spread = {slot.start: std for slot, std in candidates}
    best = cheapest_slots(
        slots,
        needed,
        energy_per_slot_kwh(request.amps, request.phases, request.voltage_between_phases_v),
        request.max_periods,
        request.fiscal,
        gap.deadline.astimezone(timezone.utc),
    )
    if best is None:
        return HistoryDecision("no_profile", weeks=profile.weeks, unknown_slots=len(candidates))

    fiscal: FiscalChoice = request.fiscal
    expected = [effective_minor_per_kwh(slot.local_major_per_kwh, fiscal) for slot in best]
    # The spread through the same conversion: the effective price one standard deviation above the median,
    # less the median's, so tax and fee (added) drop out and VAT (a factor) scales it.
    spreads = [
        effective_minor_per_kwh(slot.local_major_per_kwh + spread[slot.start], fiscal) - price
        for slot, price in zip(best, expected)
    ]
    known_mean = sum(slot.effective_minor_per_kwh for slot in known.slots) / len(known.slots)
    expected_mean = sum(expected) / len(expected)
    margin = MARGIN_STANDARD_DEVIATIONS * sum(spreads) / len(spreads)
    saving = known_mean - expected_mean

    zone = dt_util.get_time_zone(profile.tz)
    days = Counter(slot.start.astimezone(zone).isoweekday() for slot in best)
    top = max(days.values())
    # The earliest of the most frequent weekdays, so a tie reads as the first day the charge would fall on.
    weekday = next(
        slot.start.astimezone(zone).isoweekday()
        for slot in best
        if days[slot.start.astimezone(zone).isoweekday()] == top
    )
    # Whole percent of the known mean, never below 1 once there is a saving (a status line says "cheaper").
    if saving <= 0:
        percent = 0
    elif known_mean == 0:
        percent = 100
    else:
        percent = max(1, min(100, round(saving / abs(known_mean) * 100)))
    return HistoryDecision(
        "wait" if saving - margin > EPSILON_MINOR else "plan_known",
        known_mean_minor=known_mean,
        expected_mean_minor=expected_mean,
        margin_minor=margin,
        weekday=weekday,
        percent=percent,
        weeks=profile.weeks,
        unknown_slots=len(candidates),
        known_cheaper=-saving - margin > EPSILON_MINOR,
    )


def unsafe(decision: HistoryDecision) -> HistoryDecision:
    """The same decision, recorded as one that argued for waiting where waiting is not safe."""
    return replace(decision, outcome="unsafe_to_wait")


__all__ = ["HistoryDecision", "evaluate", "unknown_slots", "unsafe", "MARGIN_STANDARD_DEVIATIONS"]
