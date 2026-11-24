"""A stored day review counted again with the battery on the AC side, from the recorder's statistics (pure).

The night's review keeps its figures, not its quarter hours (those go after `plan_shadow.BUCKET_DAYS`). A review from
before the AC side (`DayReview.battery_ac` `None`) on a battery read at the battery (DC) counted the inverter's loss as
house load (`conversion_loss.py`). While its quarter hours are kept it is simply reviewed again
(`plan_runtime._review_again`). An older one (up to `plan_shadow.REVIEW_DAYS`) is rebuilt here from what Home
Assistant's recorder still has: the site's grid power and the battery's power, per five minutes where the short-term
statistics are kept (ten days by default), else per hour.

* **The quarter hours** (`rebuilt_quarters`): each period's grid without the battery, on the AC side, is
  `grid - AC(battery)` (`conversion_loss.ac_kw`, with the loss known now). A quarter hour is the mean of its three
  five-minute periods, or its hour's mean; one without both figures is left out. The cars and the house are not told
  apart: the plan's net (`house + car - sun`) is all a run needs. Each gets the day's prices with the fees as set now
  (as the house card prices a past day) and the site's import cap now.
* **What really happened** (`actual_cost`): the grid as measured over the same quarter hours, so it and the day with
  no battery cover the same time. The start and end SoC are the stored review's.
* **Again** (`rebuilt_review`): the day with no battery, self-consumption and the perfect plans (`hindsight.facit_day`,
  with the AC round trip) worked out again; the battery's own way's saving follows from them. The SpotNav modes'
  runs cannot be run again (what each planned per quarter hour is gone): their savings are kept as stored, which is
  right to the first order because the loss lay in their house and in the day with no battery alike, and only
  measured against the new yardstick. The review's own runs are moved by the loss's cost (`loss_cost`), so the
  battery's own way against self-consumption and against no battery fall by it while the modes' figures stay. The sun's
  figure is kept. The review is marked `BATTERY_AC_STATISTICS`.

What it cannot tell: an hour's mean nets a charge and a discharge in the same hour, so past the five-minute
statistics the loss is a little low; the grid from statistics may miss a few minutes SpotNav measured or the other way
round.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Final

from .conversion_loss import ac_kw
from .hindsight import BASIS_DAY, BASIS_NEXT_DAY, OWN, PLANNING_MODES, against, end_value_next_day, facit_day
from .plan_model import BatteryParams
from .plan_shadow import BATTERY_AC_STATISTICS, BUCKET, Bucket, DayReview, PriceSlot, Run
from .review_baselines import no_battery_cost

FIVE_MIN: Final = timedelta(minutes=5)
HOUR: Final = timedelta(hours=1)
#: A rebuilt day must cover at least this share of the quarter hours the review covered.
MIN_COVER_SHARE: Final = 0.9


def rebuilt_quarters(
    start: datetime,
    end: datetime,
    grid_kw: Mapping[datetime, float],
    battery_kw: Mapping[datetime, float],
    step: timedelta,
    prices: Sequence[PriceSlot],
    cap_kw: float | None,
    loss: tuple[float, float],
) -> tuple[list[Bucket], float]:
    """The day's quarter hours from [start] to [end] (UTC) as the review counts them, the battery taken out on the AC
    side (see the module), and what really happened over them (minor units). [grid_kw] and [battery_kw] are the
    periods' means (kW, import and charging positive) by their start, each [step] long (five minutes or an hour)."""
    quarters: list[Bucket] = []
    actual = 0.0
    moment = start
    while moment < end:
        if step == HOUR:
            periods = [moment.replace(minute=0)]
        else:
            periods = [moment + index * step for index in range(int(BUCKET / step))]
        price = next((p for p in prices if p.start <= moment < p.end), None)
        if price is not None and all(p in grid_kw and p in battery_kw for p in periods):
            grid = sum(grid_kw[p] for p in periods) / len(periods)
            without = sum(grid_kw[p] - ac_kw(battery_kw[p], loss) for p in periods) / len(periods)
            hours = BUCKET.total_seconds() / 3600.0
            quarters.append(
                Bucket(
                    start=moment,
                    buy=price.buy,
                    sell=price.sell,
                    cap_kw=cap_kw,
                    import_kwh=max(0.0, without) * hours,
                    export_kwh=max(0.0, -without) * hours,
                    covered_s=BUCKET.total_seconds(),
                    battery_ac_kwh=0.0,
                )
            )
            actual += grid * hours * (price.buy if grid >= 0 else price.sell)
        moment += BUCKET
    return quarters, actual


def rebuilt_review(
    review: DayReview,
    quarters: Sequence[Bucket],
    actual_cost: float,
    battery: BatteryParams,
    prices_after: Sequence[PriceSlot],
    end: datetime,
    next_end: datetime,
    day_quarters: int,
    wear: float,
    now: datetime,
    sell_margin: float | None = None,
) -> DayReview | None:
    """[review] counted again over the rebuilt [quarters] (see the module); `None` when they cover too little of it or
    the review has no day without a battery to move from."""
    if review.no_battery_cost is None or len(quarters) < MIN_COVER_SHARE * review.covered:
        return None
    nothing = no_battery_cost(quarters)
    # What the battery saved against no battery, before and now: the difference is the loss's cost.
    loss_cost = (review.no_battery_cost - review.actual.cost) - (nothing - actual_cost)
    value = end_value_next_day(prices_after, end, next_end, battery)
    kwargs = {} if sell_margin is None else {"sell_margin": sell_margin}
    stored = review.facit
    made = facit_day(
        quarters,
        battery,
        start_soc_pct=review.start_soc_pct,
        actual_cost=actual_cost,
        actual_end_soc_pct=review.actual.end_soc_pct,
        mode=review.mode,
        wear=wear,
        end_value=review.end_value if value is None else value,
        basis=BASIS_DAY if value is None else BASIS_NEXT_DAY,
        no_battery_cost=nothing,
        day_quarters=day_quarters,
        now=now,
        own_work=None if stored is None or OWN not in stored.candidates else stored.candidates[OWN].work,
        **kwargs,
    )
    if made is not None and stored is not None:
        # The modes' runs are gone: their savings as stored, against the new yardstick.
        yardstick = made.yardstick
        kept = {
            name: against(item, yardstick.saving, made.whole)
            for name, item in stored.candidates.items()
            if name in PLANNING_MODES and name not in made.candidates and yardstick is not None
        }
        made = replace(made, candidates={**made.candidates, **kept})

    def moved(run: Run) -> Run:
        return Run(run.cost - loss_cost, run.end_soc_pct)

    return replace(
        review,
        no_battery_cost=review.no_battery_cost - loss_cost,
        plan=moved(review.plan),
        self_consumption=moved(review.self_consumption),
        modes={name: moved(run) for name, run in review.modes.items()},
        facit=made if made is not None else stored,
        battery_ac=BATTERY_AC_STATISTICS,
    )
