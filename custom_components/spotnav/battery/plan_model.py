"""What the house battery plan takes and gives: pure data, no Home Assistant, no clock.

Units throughout: power in kW, energy in kWh, money in the area's **minor** unit (öre, cent), prices in minor
per kWh, as `planning.planner.effective_minor_per_kwh` gives them. A battery's power is on the AC side and signed
`+` charge, `-` discharge; the grid's is signed `+` import, `-` export.

The plan is a shadow: it says what it would do and what that would cost; nothing here writes to a device.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

#: Let the house, the car and the sun run through the battery as the inverter does by itself: the battery covers
#: a deficit and stores a surplus. The default, and the baseline every saving is measured against.
ACTION_SELF_CONSUME: Final = "self_consume"
#: Charge at a set power (`battery_kw`), from the sun first and the grid for the rest.
ACTION_GRID_CHARGE: Final = "grid_charge"
#: No discharge: the grid covers a deficit; a sun surplus is still stored.
ACTION_HOLD: Final = "hold"
#: Discharge into the house's own deficit only, and store nothing: a sun surplus is sold.
ACTION_DISCHARGE_HOUSE: Final = "discharge_house"
#: Discharge at full power and sell what the house does not use. Only when `allow_export_discharge` is set.
ACTION_EXPORT_DISCHARGE: Final = "export_discharge"

ACTIONS: Final = (
    ACTION_SELF_CONSUME,
    ACTION_HOLD,
    ACTION_DISCHARGE_HOUSE,
    ACTION_GRID_CHARGE,
    ACTION_EXPORT_DISCHARGE,
)

#: A battery's wear, in minor per kWh through it (charged plus discharged, AC side): enough to keep the plan from
#: churning for a few öre, small next to a real price spread.
DEFAULT_CYCLE_COST: Final = 5.0
#: The round trip when a battery does not state its own.
DEFAULT_ROUND_TRIP_EFFICIENCY: Final = 0.90
#: The grid of stored energy the plan searches, as steps from the lowest to the highest allowed charge.
DEFAULT_SOC_STEPS: Final = 100
#: The grid charge powers tried, as shares of the battery's rated charge power.
DEFAULT_CHARGE_SHARES: Final = (0.25, 0.5, 0.75, 1.0)
#: Slots the plan accepts at most: two days of quarter hours with room for a long day.
MAX_SLOTS: Final = 2 * 25 * 4


class PlanInputError(ValueError):
    """The plan cannot be made from these inputs; `code` says why in one word."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


@dataclass(frozen=True, slots=True)
class BatteryParams:
    """A house battery as the plan sees it.

    `usable_kwh` is what 0-100 % spans. `min_soc_pct` is the floor the plan never goes below (the inverter's own
    reserve or cut-off), `max_soc_pct` the ceiling it never charges above.
    """

    usable_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    min_soc_pct: float = 10.0
    max_soc_pct: float = 100.0
    round_trip_efficiency: float = DEFAULT_ROUND_TRIP_EFFICIENCY

    def validate(self) -> None:
        if not _finite(self.usable_kwh) or self.usable_kwh <= 0:
            raise PlanInputError("invalid_battery", "usable_kwh must be a positive number")
        if not _finite(self.max_charge_kw) or self.max_charge_kw < 0:
            raise PlanInputError("invalid_battery", "max_charge_kw must be zero or more")
        if not _finite(self.max_discharge_kw) or self.max_discharge_kw < 0:
            raise PlanInputError("invalid_battery", "max_discharge_kw must be zero or more")
        if not (_finite(self.min_soc_pct) and _finite(self.max_soc_pct)):
            raise PlanInputError("invalid_battery", "min and max SoC must be numbers")
        if not 0 <= self.min_soc_pct < self.max_soc_pct <= 100:
            raise PlanInputError("invalid_battery", "need 0 <= min_soc_pct < max_soc_pct <= 100")
        if not _finite(self.round_trip_efficiency) or not 0 < self.round_trip_efficiency <= 1:
            raise PlanInputError("invalid_battery", "round_trip_efficiency must be in (0, 1]")

    @property
    def min_kwh(self) -> float:
        return self.usable_kwh * self.min_soc_pct / 100

    @property
    def max_kwh(self) -> float:
        return self.usable_kwh * self.max_soc_pct / 100

    @property
    def one_way_efficiency(self) -> float:
        """Charge and discharge share the round trip's loss equally."""
        return math.sqrt(self.round_trip_efficiency)


@dataclass(frozen=True, slots=True)
class PlanSlot:
    """One slot of the horizon, 15 min or 1 h as its prices come.

    `house_kw` is the house's mean load without the car, `car_kw` the car's planned charge (a fixed load: the
    battery plan never moves it), `solar_kw` the mean production. `buy` is the import price with every fee SpotNav
    adds, `sell` what an exported kWh earns. `import_cap_kw` (the fuse less its margin, or the import target) and
    `export_cap_kw` bound the grid; `None` is no bound.
    """

    start: datetime
    end: datetime
    buy: float
    sell: float
    house_kw: float
    solar_kw: float = 0.0
    car_kw: float = 0.0
    import_cap_kw: float | None = None
    export_cap_kw: float | None = None

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    @property
    def net_kw(self) -> float:
        """What the site needs from the battery or the grid before either acts: `+` a deficit, `-` a surplus."""
        return self.house_kw + self.car_kw - self.solar_kw

    def validate(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise PlanInputError("invalid_slot", "slot times must be aware")
        if self.end <= self.start:
            raise PlanInputError("invalid_slot", f"slot at {self.start.isoformat()} ends before it starts")
        for name in ("buy", "sell", "house_kw", "solar_kw", "car_kw"):
            if not _finite(getattr(self, name)):
                raise PlanInputError("invalid_slot", f"{name} at {self.start.isoformat()} is not a number")
        if self.house_kw < 0 or self.solar_kw < 0 or self.car_kw < 0:
            raise PlanInputError("invalid_slot", f"a negative load or production at {self.start.isoformat()}")
        for name in ("import_cap_kw", "export_cap_kw"):
            cap = getattr(self, name)
            if cap is not None and (not _finite(cap) or cap < 0):
                raise PlanInputError("invalid_slot", f"{name} at {self.start.isoformat()} must be zero or more")


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """Everything one plan needs.

    `end_value` is what a kWh still stored at the horizon is worth, in minor per kWh; `None` takes the median buy
    price of the horizon times the round trip (`plan.default_end_value`). `cycle_cost` is the wear per kWh through
    the battery. `allow_export_discharge` lets the plan sell from the battery; off by default.
    """

    slots: tuple[PlanSlot, ...]
    battery: BatteryParams
    soc_now_pct: float
    end_value: float | None = None
    cycle_cost: float = DEFAULT_CYCLE_COST
    allow_export_discharge: bool = False
    soc_steps: int = DEFAULT_SOC_STEPS
    charge_shares: tuple[float, ...] = DEFAULT_CHARGE_SHARES

    def validate(self) -> None:
        if not self.slots:
            raise PlanInputError("no_slots", "a plan needs at least one slot")
        if len(self.slots) > MAX_SLOTS:
            raise PlanInputError("too_many_slots", f"at most {MAX_SLOTS} slots")
        self.battery.validate()
        previous: PlanSlot | None = None
        for slot in self.slots:
            slot.validate()
            if previous is not None and slot.start != previous.end:
                raise PlanInputError("gap", f"slot at {slot.start.isoformat()} does not follow the one before")
            previous = slot
        if not _finite(self.soc_now_pct) or not 0 <= self.soc_now_pct <= 100:
            raise PlanInputError("invalid_soc", "soc_now_pct must be in [0, 100]")
        if self.end_value is not None and not _finite(self.end_value):
            raise PlanInputError("invalid_end_value", "end_value must be a number")
        if not _finite(self.cycle_cost) or self.cycle_cost < 0:
            raise PlanInputError("invalid_cycle_cost", "cycle_cost must be zero or more")
        if self.soc_steps < 2:
            raise PlanInputError("invalid_steps", "soc_steps must be at least 2")
        if any(not _finite(share) or not 0 < share <= 1 for share in self.charge_shares):
            raise PlanInputError("invalid_charge_shares", "charge shares must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class SlotDecision:
    """What the plan would do in one slot, and what it expects.

    `over_cap` marks a slot whose import stays above `import_cap_kw` even with the battery discharging all it can:
    the house's own load breaks the cap there, never the plan. `curtailed_kwh` is sun the export cap would throw away.
    """

    start: datetime
    end: datetime
    action: str
    battery_kw: float
    grid_kw: float
    soc_end_pct: float
    cost: float
    over_cap: bool = False
    curtailed_kwh: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "action": self.action,
            "battery_kw": round(self.battery_kw, 3),
            "grid_kw": round(self.grid_kw, 3),
            "soc_end_pct": round(self.soc_end_pct, 1),
            "cost": round(self.cost, 2),
        }
        if self.over_cap:
            out["over_cap"] = True
        if self.curtailed_kwh > 0:
            out["curtailed_kwh"] = round(self.curtailed_kwh, 3)
        return out


@dataclass(frozen=True, slots=True)
class PlanRun:
    """One way through the horizon: the plan's, or the baseline's (self-consumption only).

    `cost` is money only (imports less exports); `wear` is the cycle cost the plan weighed but nobody pays;
    `end_soc_pct` is where the battery ends.
    """

    decisions: tuple[SlotDecision, ...]
    cost: float
    wear: float
    end_soc_pct: float

    def end_kwh(self, battery: BatteryParams) -> float:
        return battery.usable_kwh * self.end_soc_pct / 100


@dataclass(frozen=True, slots=True)
class BatteryPlan:
    """The plan and its baseline, side by side, so the saving is explicit.

    `savings` compares the two **with** the stored energy each leaves at the horizon valued at `end_value`: a plan
    that ends fuller is credited for it, one that empties the battery to look cheaper is charged for it.
    """

    plan: PlanRun
    baseline: PlanRun
    end_value: float
    battery: BatteryParams = field(repr=False)

    @property
    def savings(self) -> float:
        def net(run: PlanRun) -> float:
            return run.cost - run.end_kwh(self.battery) * self.end_value

        return net(self.baseline) - net(self.plan)

    def as_dict(self) -> dict[str, Any]:
        """For diagnostics and the debug bundle: money in minor units, two decimals."""
        return {
            "cost": round(self.plan.cost, 2),
            "baseline_cost": round(self.baseline.cost, 2),
            "savings": round(self.savings, 2),
            "end_value": round(self.end_value, 2),
            "end_soc_pct": round(self.plan.end_soc_pct, 1),
            "baseline_end_soc_pct": round(self.baseline.end_soc_pct, 1),
            "wear": round(self.plan.wear, 2),
            "slots": [decision.as_dict() for decision in self.plan.decisions],
        }
