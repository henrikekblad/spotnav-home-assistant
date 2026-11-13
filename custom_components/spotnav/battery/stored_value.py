"""The value of the energy in the battery, kept like an inventory, and each day's money split from it (pure).

The house card's money line splits a day the way the battery's own app does: what was sold, what the battery's moves
earned (arbitrage), what was bought. Arbitrage needs what the energy in the battery cost, so a running
weighted-average cost is kept, carried across days and across restarts (`as_dict`/`from_dict`):

* **Charging** adds the kWh at what they cost that quarter hour: from the grid at the buy price (every fee in it),
  from the sun at the *sell* price, what that kWh would have fetched on the grid. A quarter hour's charge counts
  from the grid first, up to what was imported (the house takes the sun first, so the battery's charge beyond the
  import came from the sun).
* **Discharging** takes the kWh out at the running average cost. Each kWh given back earns its value that quarter
  hour less that cost: the buy price for a kWh the house used, the sell price for a kWh the battery exported. The
  export from the battery is arbitrage, not "sold": a quarter hour's export counts as the battery's first, up to what
  it gave, and the rest is the sun's surplus, which alone is `sold`.
* **Losses**: the kWh are counted on the AC side (the battery's measured power), and after each quarter hour the
  stock is set to what the SoC says is stored (`soc × usable`) with the cost kept. A round trip's loss so makes the
  stored kWh fewer for the same money, which raises the average cost of the rest; it never shows as a loss of its
  own. Without a SoC or a size the AC count stands.
* **The start**: with no history, the stock is the SoC × usable at the first quarter hour, valued at the first day's
  mean buy price (`estimated`, said to be so until as many kWh as that start held have been given back, so the
  estimate has worked its way out of the stock).

Only quarter hours measured for at least half their time and priced count (`Bucket.covered`, `buy`/`sell`), each once
(`last`). Money is in the area's minor unit. Days are kept `KEEP_DAYS`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

from .. import time_zones
from .plan_shadow import Bucket

KEEP_DAYS: Final = 40
#: A stock below this (kWh) is empty: its cost goes with it, and an estimated start has left the battery.
EMPTY_KWH: Final = 0.05


@dataclass(slots=True)
class DayMoney:
    """One local day's split (minor units) and the battery's energy moved (kWh)."""

    sold: float = 0.0
    bought: float = 0.0
    arbitrage: float = 0.0
    battery_export_kwh: float = 0.0
    quarters: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "sold": round(self.sold, 2),
            "bought": round(self.bought, 2),
            "arbitrage": round(self.arbitrage, 2),
            "battery_export_kwh": round(self.battery_export_kwh, 3),
            "quarters": self.quarters,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> DayMoney | None:
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                float(raw["sold"]),
                float(raw["bought"]),
                float(raw["arbitrage"]),
                float(raw.get("battery_export_kwh", 0.0)),
                int(raw.get("quarters", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(slots=True)
class StoredValue:
    """The battery's stock (kWh) and what it cost (minor units), and each day's money (see the module)."""

    kwh: float | None = None
    cost: float = 0.0
    estimated: bool = False
    last: datetime | None = None
    days: dict[date, DayMoney] = field(default_factory=dict)
    #: The kWh of an estimated start still to be given back before the average is all measured.
    estimate_left: float = 0.0

    @property
    def average(self) -> float | None:
        """The average cost of a stored kWh (minor units), `None` without a stock."""
        if self.kwh is None or self.kwh < EMPTY_KWH:
            return None
        return self.cost / self.kwh

    def _start(self, bucket: Bucket, usable_kwh: float | None, start_price: float | None) -> None:
        soc = bucket.soc_start if bucket.soc_start is not None else bucket.soc_end
        kwh = soc / 100.0 * usable_kwh if soc is not None and usable_kwh else 0.0
        price = start_price if start_price is not None else bucket.buy or 0.0
        self.kwh, self.cost, self.estimated = kwh, kwh * price, kwh >= EMPTY_KWH
        self.estimate_left = kwh

    def step(self, bucket: Bucket, tz: str, usable_kwh: float | None, start_price: float | None = None) -> bool:
        """Count one completed quarter hour (oldest first); whether it counted."""
        if self.last is not None and bucket.start <= self.last:
            return False
        if not bucket.covered or bucket.buy is None or bucket.sell is None:
            return False
        if self.kwh is None:
            self._start(bucket, usable_kwh, start_price)
        assert self.kwh is not None
        self.last = bucket.start
        buy, sell = float(bucket.buy), float(bucket.sell)
        imported, exported = bucket.scaled(bucket.import_kwh), bucket.scaled(bucket.export_kwh)
        moved = bucket.scaled(bucket.battery_kwh)
        day = bucket.start.astimezone(time_zones.require_zone(tz)).date()
        money = self.days.setdefault(day, DayMoney())
        money.quarters += 1
        money.bought += imported * buy
        sold_kwh = exported
        if moved > 0:
            from_grid = min(moved, imported)
            self.cost += from_grid * buy + (moved - from_grid) * sell
            self.kwh += moved
        elif moved < 0:
            out = -moved
            to_grid = min(out, exported)
            to_house = out - to_grid
            average = self.average if self.average is not None else buy
            money.arbitrage += to_house * (buy - average) + to_grid * (sell - average)
            money.battery_export_kwh += to_grid
            sold_kwh -= to_grid
            self.estimate_left = max(0.0, self.estimate_left - out)
            if self.estimate_left < EMPTY_KWH:
                self.estimated = False
            taken = min(out, self.kwh)
            self.cost = max(0.0, self.cost - taken * average)
            self.kwh -= taken
        money.sold += sold_kwh * sell
        soc = bucket.soc_end
        if soc is not None and usable_kwh:
            # The SoC says what is stored: a loss leaves fewer kWh for the same cost (see the module).
            self.kwh = max(0.0, soc / 100.0 * usable_kwh)
        if self.kwh < EMPTY_KWH:
            self.kwh, self.cost, self.estimated, self.estimate_left = 0.0, 0.0, False, 0.0
        return True

    def run(
        self, buckets: Iterable[Bucket], tz: str, usable_kwh: float | None, start_price: float | None = None
    ) -> int:
        """Count the completed quarter hours not counted yet, oldest first; how many counted."""
        counted = sum(
            1 for bucket in sorted(buckets, key=lambda b: b.start) if self.step(bucket, tz, usable_kwh, start_price)
        )
        if self.days:
            newest = max(self.days)
            self.days = {day: money for day, money in self.days.items() if (newest - day).days < KEEP_DAYS}
        return counted

    def as_dict(self) -> dict[str, Any]:
        return {
            "kwh": None if self.kwh is None else round(self.kwh, 4),
            "cost": round(self.cost, 3),
            "estimated": self.estimated,
            "estimate_left": round(self.estimate_left, 3),
            "last": None if self.last is None else self.last.isoformat(),
            "days": {day.isoformat(): money.as_dict() for day, money in sorted(self.days.items())},
        }

    @classmethod
    def from_dict(cls, raw: Any) -> StoredValue:
        """What the store kept; anything malformed starts again (with the estimate)."""
        if not isinstance(raw, dict):
            return cls()
        try:
            kwh = None if raw.get("kwh") is None else float(raw["kwh"])
            last = None if raw.get("last") is None else datetime.fromisoformat(raw["last"])
            if last is not None and last.tzinfo is None:
                return cls()
            days: dict[date, DayMoney] = {}
            for key, body in (raw.get("days") or {}).items():
                money = DayMoney.from_dict(body)
                if money is not None:
                    days[date.fromisoformat(key)] = money
            return cls(
                kwh,
                float(raw.get("cost", 0.0)),
                bool(raw.get("estimated", False)),
                last,
                days,
                float(raw.get("estimate_left", 0.0)),
            )
        except (TypeError, ValueError, AttributeError):
            return cls()
