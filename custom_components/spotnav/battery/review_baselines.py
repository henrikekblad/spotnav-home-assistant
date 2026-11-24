"""The day's review in the terms a battery's own app uses: against no battery, and what the sun gave. Pure.

`plan_shadow.review_day` compares ways of running the battery (the plan, each mode, plain self-consumption, what really
happened) on the same measured quarter hours. Those figures say which way is best; they do not say what the battery
earned, which is what the battery's own app (Sigenergy's, say) reports, against another baseline. This module adds two
baselines from the same measured quarter hours, no setting:

* **Against no battery** (`battery_saving`): what the day really cost (`actual`: import at the buy price less export at
  the sell price) against the same day with no battery at all. Without the battery the house and the car draw their
  load from the grid and the sun is still there, so each quarter hour's grid is `import - export - battery` (the
  battery's own charge taken away: its discharge added back; the battery on the AC side, `Bucket.battery_ac`, so the
  inverter's loss on a DC-side reading is the battery's and not the house's), bought at the buy price when it is positive and sold at
  the sell price when it is negative. Positive is money the battery saved. The energy left in the battery at the end of
  the day is *not* counted here (the battery apps do not count it either; `DayReview.actual_saving` is the figure that
  does).
* **What the sun gave** (`sun_gave`): the day with the sun and no battery against the day with neither: the grid of
  each quarter hour without the sun is that with it plus the sun's production, so the sun's value is the self-used
  sun at the buy price plus the exported sun at the sell price (each quarter hour's net, which is how the meter
  settles it). The sun and the battery together are against nothing: `battery_saving + sun_gave`.

The quarter hours are the review's own (measured at least half, with their prices). The sun's production is not
measured by the plan itself: it comes from the Energy dashboard's solar statistics, per five minutes where the recorder
still has them and else the hour spread evenly over its quarter hours, so a quarter hour without a production figure
counts in the battery's figure and not the sun's (`sun_quarters` against `covered` says so; a partial day likewise,
`covered` against the day's quarter hours).

What it cannot tell: the grid of the day without the battery is the measured one less the battery's power on the AC
side, so a conversion loss that is assumed rather than learnt (`conversion_loss.py`) is only as good as the
assumption, and a limit that the missing battery would have imposed on the sun (the export
limit) are not in it; the quarter hour's net is taken over the whole quarter hour while the actual import and export
are summed sample by sample, so a quarter hour that both imported and exported reads a little dearer in `actual` than
in the baseline.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .plan_shadow import Bucket

QUARTER: Final = timedelta(minutes=15)
HOUR: Final = timedelta(hours=1)
FIVE_MIN: Final = timedelta(minutes=5)


def grid_cost(kwh: float, buy: float, sell: float) -> float:
    """What a quarter hour's net grid energy costs (minor units): bought at [buy] when positive, sold at [sell] when
    negative."""
    return kwh * buy if kwh >= 0 else kwh * sell


def no_battery_cost(buckets: Iterable[Bucket]) -> float:
    """What the quarter hours would have cost with no battery (see the module); [buckets] are the usable ones."""
    total = 0.0
    for bucket in buckets:
        grid = bucket.scaled(bucket.import_kwh - bucket.export_kwh - bucket.battery_ac)
        total += grid_cost(grid, float(bucket.buy), float(bucket.sell))  # type: ignore[arg-type]
    return total


def sun_by_quarter(five_minute: Mapping[datetime, float], hourly: Mapping[datetime, float]) -> dict[datetime, float]:
    """The sun's production per UTC quarter hour (kWh): the sum of its three five-minute figures when it has all of
    them, else its hour's production spread evenly over the hour's four quarters."""
    out: dict[datetime, float] = {}
    for hour, kwh in hourly.items():
        hour = hour.astimezone(timezone.utc)
        for index in range(4):
            out[hour + index * QUARTER] = kwh / 4
    for quarter in {_quarter_of(moment) for moment in five_minute}:
        parts = [five_minute.get(quarter + index * FIVE_MIN) for index in range(3)]
        if all(part is not None for part in parts):
            out[quarter] = sum(parts)  # type: ignore[arg-type]
    return out


def _quarter_of(moment: datetime) -> datetime:
    moment = moment.astimezone(timezone.utc)
    return moment.replace(minute=moment.minute - moment.minute % 15, second=0, microsecond=0)


def sun_gave(buckets: Iterable[Bucket], sun_kwh: Mapping[datetime, float]) -> tuple[float, int]:
    """What the sun gave over the quarter hours that have a production figure (minor units) and how many those are."""
    total = 0.0
    quarters = 0
    for bucket in buckets:
        sun = sun_kwh.get(bucket.start)
        if sun is None or sun < 0:
            continue
        grid = bucket.scaled(bucket.import_kwh - bucket.export_kwh - bucket.battery_ac)
        buy, sell = float(bucket.buy), float(bucket.sell)  # type: ignore[arg-type]
        total += grid_cost(grid + sun, buy, sell) - grid_cost(grid, buy, sell)
        quarters += 1
    return total, quarters
