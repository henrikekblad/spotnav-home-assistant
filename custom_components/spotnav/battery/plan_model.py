"""What the house battery plan takes and gives: pure data, no Home Assistant, no clock.

Units throughout: power in kW, energy in kWh, money in the area's **minor** unit (öre, cent), prices in minor
per kWh, as `planning.planner.effective_minor_per_kwh` gives them. A battery's power is on the AC side and signed
`+` charge, `-` discharge; the grid's is signed `+` import, `-` export.

The plan is a shadow: it says what it would do and what that would cost; nothing here writes to a device.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
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
#: Discharge just enough to bring the grid down to the slot's soft ceiling under a demand charge (`soft_cap_kw`), or
#: to the day's peak so far where that is higher, and no more: the rest of the stored energy is kept for later. Only in a slot with a priced soft ceiling; no executor
#: runs it yet (the demand variant is a shadow, `plan_shadow.VARIANT_DEMAND`).
ACTION_SHAVE: Final = "shave"

ACTIONS: Final = (
    ACTION_SELF_CONSUME,
    ACTION_HOLD,
    ACTION_DISCHARGE_HOUSE,
    ACTION_GRID_CHARGE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_SHAVE,
)

#: A battery's wear per kWh through it, each way (charged, and again discharged; AC side), in the area's minor unit:
#: one constant, not a setting (`plans/research_integrations/battery_cycle_cost_and_modes_2026-11-13.md` part 1).
#: 15 öre is about 60 % of a 2 500 kr/kWh LFP bank's replacement cost over 6 000 cycles to 70 %; the other
#: currencies are the same money at the rates of November 2026, rounded. Every currency the area catalogue holds is
#: here (a test keeps it so).
WEAR_MINOR_PER_KWH: Final[dict[str, float]] = {
    "SEK": 15.0,
    "NOK": 16.0,
    "DKK": 10.0,
    "EUR": 1.4,
    "GBP": 1.2,
    "CHF": 1.3,
    "PLN": 6.0,
    "CZK": 35.0,
    "RON": 7.0,
    "HUF": 540.0,
}
#: The wear a request weighs when it is not told (the SEK figure; the shell passes the area's, `wear_minor_per_kwh`).
DEFAULT_CYCLE_COST: Final = WEAR_MINOR_PER_KWH["SEK"]
#: The round trip when a battery does not state its own.
DEFAULT_ROUND_TRIP_EFFICIENCY: Final = 0.90
#: The grid of stored energy the plan searches, as steps from the lowest to the highest allowed charge.
DEFAULT_SOC_STEPS: Final = 100
#: The grid charge powers tried, as shares of the battery's rated charge power.
DEFAULT_CHARGE_SHARES: Final = (0.25, 0.5, 0.75, 1.0)
#: The day's highest import under a demand charge is tracked at this many steps between the day's lowest soft ceiling
#: and the highest import its slots can reach (`plan.py`, the demand charge).
DEFAULT_PEAK_STEPS: Final = 8
#: Slots the plan accepts at most: two days of quarter hours with room for a long day.
MAX_SLOTS: Final = 2 * 25 * 4


#: The battery modes (one per site; `plans/v3_0_wave3.md` W3-M). Self-consumption: SpotNav never plans the battery,
#: only its guards act (the fuse, the hold while a car charges from the grid). Balanced: the battery may buy cheap
#: and spend in the house, cycles less (double wear), and sells only surplus and only when it is highly profitable
#: (the spread gate: `SELL_MARGIN_MAJOR` per kWh sold, the house's need until the next cheap hours kept first; owner
#: 2026-11-19, like Sigenergy's "Balanced Energy"). Max profit: every action, the true wear, a sale wherever it pays.
#: Every mode keeps the battery's own floor (a reserve for backup is the battery's setting, which the facts read).
MODE_SELF_CONSUMPTION: Final = "self_consumption"
MODE_BALANCED: Final = "balanced"
MODE_MAX_PROFIT: Final = "max_profit"
MODES: Final = (MODE_SELF_CONSUMPTION, MODE_BALANCED, MODE_MAX_PROFIT)
DEFAULT_MODE: Final = MODE_BALANCED

#: How a mode may sell from the battery: never, only through the spread gate, or wherever the plan finds it pays.
EXPORT_OFF: Final = "off"
EXPORT_GATE: Final = "gate"
EXPORT_FREE: Final = "free"

#: The spread gate's margin: what a kWh sold from the battery must earn beyond what keeping it is worth (the stored
#: kWh's value to the plan, the round trip and the wear) before Balanced sells it: "highly profitable". 1.00 SEK in
#: each area currency's major unit, rounded at 2026 rates (every area currency has 100 minor units to the major one,
#: `pricing/areas.json`); one constant, not a setting. Every currency the area catalogue holds is here (a test keeps
#: it so); a currency not listed takes the SEK figure, as the wear does.
SELL_MARGIN_MAJOR: Final[dict[str, float]] = {
    "SEK": 1.00,
    "NOK": 1.00,
    "DKK": 0.70,
    "EUR": 0.10,
    "CHF": 0.10,
    "GBP": 0.10,
    "PLN": 0.40,
    "CZK": 2.50,
    "RON": 0.50,
    "HUF": 35.0,
}
_MINOR_PER_MAJOR: Final = 100.0
#: The margin a request weighs when it is not told (the SEK figure, in öre).
DEFAULT_SELL_MARGIN: Final = SELL_MARGIN_MAJOR["SEK"] * _MINOR_PER_MAJOR


@dataclass(frozen=True, slots=True)
class BatteryMode:
    """A mode as a row of the plan's parameters, no code path of its own.

    `wear_factor` multiplies the currency's wear (`WEAR_MINOR_PER_KWH`). Every mode keeps the battery's own floor.
    With `export` at `EXPORT_GATE` (the spread gate), a kWh sold from the battery is weighed the currency's sell
    margin (`SELL_MARGIN_MAJOR`) below its sell price, so a sale must clear the stored kWh's value, both ways' wear
    and the margin together; and the house comes first: a sale never takes the battery below what the house needs
    until the next cheap hours (`PlanRequest.export_reserve`). Both fall out of the search, never a filter after it.
    """

    name: str
    plans: bool
    export: str
    wear_factor: float

    def wear(self, base: float) -> float:
        return base * self.wear_factor

    def hurdle(self, margin: float) -> float:
        """The margin weighed on each kWh sold from the battery: [margin] behind the gate, none otherwise."""
        return margin if self.export == EXPORT_GATE else 0.0

    @property
    def house_first(self) -> bool:
        """Whether a sale keeps the house's need until the next cheap hours (the gate's)."""
        return self.export == EXPORT_GATE


#: Balanced's multiple of the wear (the backtest in `plans/battery_plan_backtest/README.md` weighs it). No reserve
#: of its own beyond the house's need before a sale (owner 2026-11-13: the modes differ in arbitrage, not in reserve).
BALANCED_WEAR_FACTOR: Final = 2.0

MODE_DEFS: Final[dict[str, BatteryMode]] = {
    MODE_SELF_CONSUMPTION: BatteryMode(MODE_SELF_CONSUMPTION, plans=False, export=EXPORT_OFF, wear_factor=0.0),
    MODE_BALANCED: BatteryMode(MODE_BALANCED, plans=True, export=EXPORT_GATE, wear_factor=BALANCED_WEAR_FACTOR),
    MODE_MAX_PROFIT: BatteryMode(MODE_MAX_PROFIT, plans=True, export=EXPORT_FREE, wear_factor=1.0),
}


def sell_margin_minor_per_kwh(currency: str | None) -> float:
    """The spread gate's margin per kWh sold in [currency]'s minor unit; the SEK figure for one not listed."""
    major = SELL_MARGIN_MAJOR.get((currency or "").upper())
    return DEFAULT_SELL_MARGIN if major is None else major * _MINOR_PER_MAJOR


def wear_minor_per_kwh(currency: str | None) -> float | None:
    """The wear per kWh each way in [currency]'s minor unit; `None` for a currency the table does not hold."""
    if not currency:
        return None
    return WEAR_MINOR_PER_KWH.get(currency.upper())


def mode_named(name: object) -> BatteryMode | None:
    """The mode called [name] (a site entry's `battery_mode`), or `None` for anything else."""
    return MODE_DEFS.get(name) if isinstance(name, str) else None


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
    `export_cap_kw` bound the grid; `None` is no bound. `grid_charge_cap_kw` is the most the battery may be charged
    from the grid (the HA-down cap control obeys, `gate.grid_charge_cap_kw`), as the battery's total charge power as
    control commands it (the sun's surplus included): a cap not above the surplus leaves the surplus alone, which is a
    hold. The sun's own charge is never held back. `None` is no bound (the plant's own import limit guards the fuse),
    0 no grid charge.

    `soft_cap_kw` and `peak_price` are the hour's soft ceiling under a demand charge (`planning/demand_cost.py`): a
    day's highest import above `soft_cap_kw` costs `peak_price` (minor per kW above it) on top of the energy, once a
    day. `peak_day` is the day whose peak the slot counts toward (the day the demand charge counts its hour under);
    `None` is a day of its own. `None` (the default) or a price of 0 changes nothing.
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
    grid_charge_cap_kw: float | None = None
    soft_cap_kw: float | None = None
    peak_price: float = 0.0
    peak_day: date | None = None

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
        for name in ("import_cap_kw", "export_cap_kw", "soft_cap_kw", "grid_charge_cap_kw"):
            cap = getattr(self, name)
            if cap is not None and (not _finite(cap) or cap < 0):
                raise PlanInputError("invalid_slot", f"{name} at {self.start.isoformat()} must be zero or more")
        if not _finite(self.peak_price) or self.peak_price < 0:
            raise PlanInputError("invalid_slot", f"peak_price at {self.start.isoformat()} must be zero or more")

    @property
    def priced_ceiling(self) -> bool:
        """Whether an import above the slot's soft ceiling costs anything."""
        return self.soft_cap_kw is not None and self.peak_price > 0


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """Everything one plan needs.

    `end_value` is what a kWh still stored at the horizon is worth, in minor per kWh; `None` takes the median buy
    price of the horizon times the round trip (`plan.default_end_value`). `cycle_cost` is the wear per kWh through
    the battery. `allow_export_discharge` lets the plan sell from the battery; off by default. `export_hurdle`
    (minor per kWh) is weighed against every kWh the battery sells: the spread gate's margin (`BatteryMode`).
    `export_reserve` (the gate's house first): a sale never takes the battery below what the house's deficit needs
    from the next slot until the next cheap hours (`plan.house_reserve_kwh`). `peak_now_kw` is the running day's
    highest counted import so far under a demand charge (kW): the first slot's day starts from it; `None` is none.
    `peak_steps`: the steps the day's peak is tracked at (`DEFAULT_PEAK_STEPS`).
    """

    slots: tuple[PlanSlot, ...]
    battery: BatteryParams
    soc_now_pct: float
    end_value: float | None = None
    cycle_cost: float = DEFAULT_CYCLE_COST
    allow_export_discharge: bool = False
    export_hurdle: float = 0.0
    export_reserve: bool = False
    soc_steps: int = DEFAULT_SOC_STEPS
    charge_shares: tuple[float, ...] = DEFAULT_CHARGE_SHARES
    peak_now_kw: float | None = None
    peak_steps: int = DEFAULT_PEAK_STEPS

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
        if not _finite(self.export_hurdle) or self.export_hurdle < 0:
            raise PlanInputError("invalid_export_hurdle", "export_hurdle must be zero or more")
        if self.soc_steps < 2:
            raise PlanInputError("invalid_steps", "soc_steps must be at least 2")
        if any(not _finite(share) or not 0 < share <= 1 for share in self.charge_shares):
            raise PlanInputError("invalid_charge_shares", "charge shares must be in (0, 1]")
        if self.peak_now_kw is not None and (not _finite(self.peak_now_kw) or self.peak_now_kw < 0):
            raise PlanInputError("invalid_peak", "peak_now_kw must be zero or more")
        if self.peak_steps < 1:
            raise PlanInputError("invalid_steps", "peak_steps must be at least 1")


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
    #: What the slot's import adds to the demand charge by raising its day's peak above the soft ceiling (minor
    #: units; 0 without one).
    demand: float = 0.0

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
        if self.demand > 0:
            out["demand"] = round(self.demand, 2)
        return out


@dataclass(frozen=True, slots=True)
class PlanRun:
    """One way through the horizon: the plan's, or the baseline's (self-consumption only).

    `cost` is money only (imports less exports); `wear` is the cycle cost the plan weighed but nobody pays, and
    `hurdle` the spread gate's margin on what the battery sold (weighed, never paid); `end_soc_pct` is where the
    battery ends. `demand` is what the imports above the slots' soft ceilings add to the demand charge (money, but
    reported apart from `cost`; 0 without a priced ceiling).
    """

    decisions: tuple[SlotDecision, ...]
    cost: float
    wear: float
    end_soc_pct: float
    hurdle: float = 0.0
    demand: float = 0.0

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
    #: The first slot keeps the action a plan before had for it, as the new one gained too little (`keep_gain`, on
    #: the objective, minor units; `None` when there was none to keep).
    kept: bool = False
    keep_gain: float | None = None

    @property
    def savings(self) -> float:
        def net(run: PlanRun) -> float:
            return run.cost - run.end_kwh(self.battery) * self.end_value

        return net(self.baseline) - net(self.plan)

    def as_dict(self) -> dict[str, Any]:
        """For diagnostics and the debug bundle: money in minor units, two decimals."""
        out: dict[str, Any] = {
            "cost": round(self.plan.cost, 2),
            "baseline_cost": round(self.baseline.cost, 2),
            "savings": round(self.savings, 2),
            "end_value": round(self.end_value, 2),
            "end_soc_pct": round(self.plan.end_soc_pct, 1),
            "baseline_end_soc_pct": round(self.baseline.end_soc_pct, 1),
            "wear": round(self.plan.wear, 2),
            "min_soc_pct": round(self.battery.min_soc_pct, 1),
            "slots": [decision.as_dict() for decision in self.plan.decisions],
        }
        if self.plan.hurdle > 0:
            out["export_hurdle"] = round(self.plan.hurdle, 2)
        if self.plan.demand > 0 or self.baseline.demand > 0:
            out["demand"] = round(self.plan.demand, 2)
            out["baseline_demand"] = round(self.baseline.demand, 2)
        if self.keep_gain is not None:
            out["kept"] = self.kept
            out["keep_gain"] = round(self.keep_gain, 2)
        return out
