"""The reviewed days against their perfect plans, summed for a card (`battery/hindsight.py`). Pure.

One block, the same in `spotnav/get_house` (the week's reviewed days, and a past day's own) and in
`spotnav/get_house_history` (a day, month or year):

    {days, provisional, partial, mode, saving, shares: {own, self_consumption, balanced, max_profit},
     savings: {own, self_consumption, balanced, max_profit}, clamped: [name],
     work: {own, self_consumption, balanced, max_profit, perfect: {discharged_kwh, cycles, switches, grid_charge_kwh,
            to_grid_kwh} | null},
     temperature: {days, mean_c, max_c, minutes_above} | null} | null

* `days`: the whole days that have a perfect plan (at least 90 % of their quarter hours measured, `hindsight.py`);
  only they count in every figure below. `provisional` of them valued the energy left at midnight at the day's own
  end value (the next day's prices were not held yet); `partial` the days left out as incomplete; `mode` the mode
  whose rules the perfect plan kept (the site's, the most days').
* `saving`: the perfect plan's saving against no battery, summed (minor units). `savings`: each way's, summed over the
  days it ran (`None` where it ran none): what really happened (`own`: the battery's own optimiser on a battery that
  runs its own way), self-consumption and each SpotNav mode.
* `shares`: each way's summed saving over the summed perfect saving it is measured against (its own mode's for a
  SpotNav mode, the site's for the others), in percent, clamped to 0-100 (`clamped` names those that were). `None`
  where the way never ran, and where the perfect plan saved under `MIN_PERFECT_SAVING` a day over its days (5 kr a
  day: too small a difference to compare). Summed over days before dividing, the day boundary washes out: a single
  day counts the energy left at midnight (`hindsight.py`).
* `work`: the battery's work summed over the days, per way and for the perfect plan (the site's mode's).
* `temperature`: the cells' temperature over the days that have it (mean weighted by the minutes read).

`None` when no day has a perfect plan (whole or partial) nor a cell temperature.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from ..battery.hindsight import BASIS_DAY, CANDIDATES, MIN_PERFECT_SAVING, PERFECT, Work, share_of
from ..battery.plan_shadow import DayReview

PERFECT_KEYS: Final = ("days", "provisional", "partial", "mode", "saving", "shares", "savings", "clamped", "work", "temperature")
WORK_WAYS: Final = (*CANDIDATES, PERFECT)
WORK_KEYS: Final = ("discharged_kwh", "cycles", "switches", "grid_charge_kwh", "to_grid_kwh")
TEMPERATURE_KEYS: Final = ("days", "mean_c", "max_c", "minutes_above")


def _work_sum(items: Sequence[Work]) -> dict[str, Any] | None:
    if not items:
        return None
    return Work(
        discharged_kwh=sum(item.discharged_kwh for item in items),
        cycles=sum(item.cycles for item in items),
        switches=sum(item.switches for item in items),
        grid_charge_kwh=sum(item.grid_charge_kwh for item in items),
        to_grid_kwh=sum(item.to_grid_kwh for item in items),
    ).as_dict()


def _temperature(reviews: Sequence[DayReview]) -> dict[str, Any] | None:
    days = [review.cell_temperature for review in reviews if review.cell_temperature is not None]
    minutes = sum(item.minutes for item in days)  # type: ignore[union-attr]
    if not days or minutes <= 0:
        return None
    return {
        "days": len(days),
        "mean_c": round(sum(item.mean_c * item.minutes for item in days) / minutes, 1),  # type: ignore[union-attr]
        "max_c": round(max(item.max_c for item in days), 1),  # type: ignore[union-attr]
        "minutes_above": sum(item.minutes_above for item in days),  # type: ignore[union-attr]
    }


def perfect_block(reviews: Sequence[DayReview]) -> dict[str, Any] | None:
    """The block (see the module) over [reviews]."""
    made = [review.facit for review in reviews if review.facit is not None and review.facit.saving is not None]
    facits = [facit for facit in made if facit.whole]
    temperature = _temperature(reviews)
    if not made and temperature is None:
        return None
    shares: dict[str, float | None] = {}
    savings: dict[str, float | None] = {}
    clamped: list[str] = []
    work: dict[str, Any] = {}
    for name in CANDIDATES:
        ran = [facit.candidates[name] for facit in facits if name in facit.candidates]
        if not ran:
            shares[name] = savings[name] = work[name] = None
            continue
        saved = sum(item.saving for item in ran)
        share, was_clamped = share_of(saved, sum(item.perfect for item in ran), MIN_PERFECT_SAVING * len(ran))
        shares[name] = None if share is None else round(share, 1)
        savings[name] = round(saved, 2)
        if was_clamped:
            clamped.append(name)
        work[name] = _work_sum([item.work for item in ran])
    headline = [facit.perfect[facit.mode] for facit in facits]
    work[PERFECT] = _work_sum([item.work for item in headline])
    modes = [facit.mode for facit in facits]
    return {
        "days": len(facits),
        "provisional": sum(1 for facit in facits if facit.basis == BASIS_DAY),
        "partial": len(made) - len(facits),
        "mode": max(reversed(modes), key=modes.count) if modes else None,
        "saving": round(sum(item.saving for item in headline), 2) if facits else None,
        "shares": shares,
        "savings": savings,
        "clamped": clamped,
        "work": work,
        "temperature": temperature,
    }
