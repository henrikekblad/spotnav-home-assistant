"""No solar export at a negative sell price: the rule, its shadow and what it would have saved. Pure.

The rule (owner, 2026-11-15): a quarter hour whose sell price is below zero is a **curtail** slot. The sell price is
what an exported kWh earns (`plan.sell_prices`: the spot price plus the site's export credit, so with a 5 öre credit
the spot must be below -5 öre). In a curtail slot the site's export limit goes to 0 kW; when the price turns positive
again the limit goes back to the value found. The sun still covers the house, the car and the battery; the inverter
curtails only the rest.

* **Beside the battery plan, not in its search.** `curtail_slots` caps a curtail slot's export at 0
  (`PlanSlot.export_cap_kw`), so the plan sees the sun it cannot sell and stores it first (`plan._apply`: sun that
  still does not fit is `curtailed_kwh`, never sold at the negative price). A site without a battery has no plan;
  the rule still holds.
* **Control** is the executor's action `curtail_export` (`commands.ACTION_CURTAIL_EXPORT`) on the brand's export
  control (`commands.EXPORT_CONTROLS`, Sigenergy only), the one write route: written only by SpotNav, restored to
  what was there, given up when someone else changes it, an `unknown` read-back never a success, `unknown` never
  written back, the site's day write cap shared with the battery's control. A battery that runs its own optimiser
  (Sigenergy AI mode) or that another system drives is only watched, as for the battery. No setting of its own: it
  belongs to the battery control opt-in when that comes.
* **Shadow first** (`CurtailShadow`): in this release nothing writes. Each look says whether the slot now is a
  curtail slot; on entering one the shell logs what the executor would write (`executor.preview`) or why it would
  not, on leaving it what the hand-back would write; and it counts, per local day, the kWh the site exported in
  curtail slots and what they cost (kWh × -sell price): what the rule would have saved. Days the control could not
  have run (watch-only, entity missing) are counted apart (`blocked_kwh`), so the saving is never claimed for them.

What the shadow cannot tell: whether the inverter would have obeyed, and how fast.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Final

from .plan_model import PlanSlot
from .shadow import parse_aware_datetime

#: A sell price (minor per kWh) below this is a curtail slot.
CURTAIL_BELOW: Final = 0.0
#: A gap between two looks longer than this (s) adds nothing to the export counted.
MAX_GAP_S: Final = 60.0
#: Days of counts kept.
TALLY_DAYS: Final = 30


def curtails(sell: float | None) -> bool:
    """Whether a slot with this sell price (minor per kWh) is a curtail slot; an unknown price is not."""
    return sell is not None and math.isfinite(sell) and sell < CURTAIL_BELOW


def curtail_slots(slots: Iterable[PlanSlot]) -> tuple[PlanSlot, ...]:
    """The plan's slots with every curtail slot's export capped at 0 kW."""
    return tuple(
        replace(slot, export_cap_kw=0.0) if curtails(slot.sell) else slot
        for slot in slots
    )


def would_save(export_kwh: float, sell: float) -> float:
    """What not exporting `export_kwh` at `sell` (minor per kWh, below zero) saves (minor units)."""
    return max(export_kwh, 0.0) * max(-sell, 0.0)


@dataclass(slots=True)
class DayCount:
    """One local day's export in curtail slots: all of it, and the part the control could not have stopped."""

    kwh: float = 0.0
    saving: float = 0.0
    blocked_kwh: float = 0.0
    blocked_saving: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "kwh": round(self.kwh, 4),
            "saving": round(self.saving, 3),
            "blocked_kwh": round(self.blocked_kwh, 4),
            "blocked_saving": round(self.blocked_saving, 3),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> DayCount | None:
        if not isinstance(raw, Mapping):
            return None
        values = [raw.get(key, 0.0) for key in ("kwh", "saving", "blocked_kwh", "blocked_saving")]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in values):
            return None
        return cls(*(float(v) for v in values))


@dataclass(frozen=True, slots=True)
class CurtailLook:
    """One look: the time, the local day, the sell price now (minor per kWh, `None` unknown), the site's grid power
    (kW, + import, `None` unknown) and, in a curtail slot, why the control would not run (`None`: it would)."""

    now: datetime
    day: str
    sell: float | None
    grid_kw: float | None
    blocker: str | None = None


@dataclass(slots=True)
class CurtailShadow:
    """The rule in shadow: whether a curtail slot runs, since when, and the days' counts."""

    curtailing: bool = False
    since: datetime | None = None
    blocker: str | None = None
    last_at: datetime | None = None
    days: dict[str, DayCount] = field(default_factory=dict)

    def step(self, look: CurtailLook) -> str | None:
        """One look; returns `enter` or `leave` when a curtail slot starts or ends, else `None`."""
        now = look.now
        wanted = curtails(look.sell)
        if self.curtailing and self.last_at is not None and look.grid_kw is not None and math.isfinite(look.grid_kw):
            gap = (now - self.last_at).total_seconds()
            if 0 < gap <= MAX_GAP_S and look.grid_kw < 0 and look.sell is not None and curtails(look.sell):
                kwh = -look.grid_kw * gap / 3600.0
                count = self.days.setdefault(look.day, DayCount())
                count.kwh += kwh
                count.saving += would_save(kwh, look.sell)
                if self.blocker is not None:
                    count.blocked_kwh += kwh
                    count.blocked_saving += would_save(kwh, look.sell)
        self.last_at = now
        edge: str | None = None
        if wanted and not self.curtailing:
            self.curtailing, self.since, edge = True, now, "enter"
        elif not wanted and self.curtailing:
            self.curtailing, self.since, edge = False, None, "leave"
        self.blocker = look.blocker if self.curtailing else None
        if len(self.days) > TALLY_DAYS:
            for day in sorted(self.days)[: len(self.days) - TALLY_DAYS]:
                del self.days[day]
        return edge

    def totals(self) -> dict[str, float]:
        """The days kept summed."""
        out = DayCount()
        for count in self.days.values():
            out.kwh += count.kwh
            out.saving += count.saving
            out.blocked_kwh += count.blocked_kwh
            out.blocked_saving += count.blocked_saving
        return out.as_dict()

    def as_dict(self) -> dict[str, Any]:
        return {
            "curtailing": self.curtailing,
            "since": None if self.since is None else self.since.isoformat(),
            "blocker": self.blocker,
            "last_at": None if self.last_at is None else self.last_at.isoformat(),
            "days": {day: count.as_dict() for day, count in sorted(self.days.items())},
        }

    @classmethod
    def from_dict(cls, raw: Any) -> CurtailShadow:
        if not isinstance(raw, Mapping):
            return cls()
        days: dict[str, DayCount] = {}
        if isinstance(raw.get("days"), Mapping):
            for day, item in raw["days"].items():
                count = DayCount.from_dict(item)
                if count is not None and isinstance(day, str):
                    days[day] = count
        since = parse_aware_datetime(raw.get("since"))
        curtailing = raw.get("curtailing") is True
        blocker = raw.get("blocker")
        return cls(
            curtailing=curtailing,
            since=since if curtailing else None,
            blocker=blocker if curtailing and isinstance(blocker, str) else None,
            last_at=parse_aware_datetime(raw.get("last_at")),
            days=dict(sorted(days.items())[-TALLY_DAYS:]),
        )
