"""Today's energy flow for the house card's Sankey diagram (SpotNav 3.0 preview, shadow): sources on one side, uses
on the other, and how much went from each source to each use.

Pure: the shell (`api/house.py`) reads the hours (`EnergyHour`) and hands them over; nothing is read or written here.

**What is measured** per period of the day (`EnergyHour`): the sun's production, the grid's import and export and the
home battery's charge and discharge, each from the Energy dashboard's statistics (`site/energy_statistics.py`), and
the cars' energy (the site's chargers' energy registers, else what the battery plan measured in the period). A day
within the recorder's short-term statistics (5-minute rows, kept `keep_days`) is read by the quarter hour
(`quarter_hours`: the sum of a quarter's three 5-minute rows; the running quarter counts its finished ones), so the
flow follows the day every five minutes; an older day, and a finished hour whose quarters are not all there, by the
hour (`energy_hours`). The
house is the balance: `sun + import + discharge - export - charge - cars - heat`, never below 0 (when the meters
disagree and it would be, the missing energy is taken off the uses in the order export, battery charge, cars, heat).
**Heat** is the heat pump's own use, where its meter is found or set (`site/heat_meter.py`, the recorder's hourly
statistics of it), and the heat zones' own (electric floor heating, an air-to-air heat pump: a sensor on each chosen
zone's device, `site/heat_zones.py`), each counted once: a use of its own, split out of the house (which drops by
the same energy); an hour without its figure leaves it in the house. **The pool's heat pump and circulation, the
spa and the sauna** are parts of heat too (`with_heat_parts`): the energy the pool and spa shadow counts per quarter
hour (`pool/runtime.energy_quarters`: the part's own power sensor where it is trusted, else its on time at its power
as it counts), added to each period's heat, so the house drops by the same energy and the heat card's Use tab shows the
same figures; a part whose device the heat pump's meter or a zone already reads is left out (`api/house.py`), and a
period without a part's quarters leaves it in the house. `heat_sources` is how many sources it holds (the meter, each
zone and each such part counted), so the card offers their split (`spotnav/get_house_loads` `kind: "heat"`) only
when there is more than one.

**The battery on the AC side**: a hybrid that reads its battery at the battery (the DC side, `adapters`:
`dc_power`; its Energy dashboard meters are the battery's own counters) leaves the inverter's loss out of them, and
the balance would put it in the house (1-3 kWh a day on the owner's plant). Where the battery is read so, each period's
charge and discharge are turned to the AC side before the flow is allocated (`on_ac_side`, the battery plan's learnt
loss, else the assumed one, `battery/conversion_loss.py`): the part of a charge the grid's import can have given at
the way-in loss, the rest (the sun's, over the DC bus) and the discharge at the way-out loss. So the battery's nodes
and flows are on the AC side and the house is right; what was taken out of the house is `inverter_loss_kwh` (each
period's `EnergyHour.inverter_loss`, summed), which the card says in one line under the diagram. `null` where the
battery is read on the AC side or the site has none. A past day is turned with the loss known now, as the review is
(`battery/review_rebuild.py`). The sun stays as read (no adapter marks its solar power as DC).

**What is not measured** is which source fed which use: no meter says whether the sun or the grid charged the
battery. The flows are allocated by one rule, period by period (quarter hour or hour) so a sunny midday quarter and a
night of grid charging do not mix:

1. **The sun** goes to the house, then heat, then the cars, then the battery, then the grid (export).
2. **The battery's discharge** goes first to whatever export the sun left (the grid never exports, so the rest of the
   export can only be the battery's), then to the house, then heat, then the cars.
3. **The grid's import** goes first to whatever battery charge the sun left (the battery does not charge itself), then
   to the house, then heat, then the cars.
4. Whatever is left can only be a battery that both charged and discharged in the hour, or a grid that both imported
   and exported: it is netted out (left out of both sides), so the diagram never shows the battery feeding itself.

So every node balances: a source's figure is the sum of its flows, and so is a use's.

**Shape** (`energy_flow`): `nodes` and `links` are lists with a `role` and a `side`, not fixed fields, so a source or a
use can be added without a contract break. A node is left out when the site has nothing to measure it with (no solar
statistics: no sun node; no battery statistics: neither battery node; no heat meter: no heat node); a use with no
energy today stays in with 0. `hours` counts the periods allocated, `car_hours` and `heat_hours` those that had the
cars' and the heat figure, `step_min` is the period (15 for a day read by the quarter hour, else 60) and `through`
the end of the last one. A day with no period yet (today's first minutes) is an empty flow: `hours` 0, every node 0,
no links, `through` the day's start (`energy_flow`'s `since`).
A bidirectional charger (V2H/V2G, Sigenergy's DC charger) would add the car as a source: `role` `car` on the `source`
side, measured by the charger's export register into `EnergyHour.car_out`, allocated after the battery by the
battery's rule (export first, then the house); `ALLOCATION` holds the order and is where it would go.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Final

#: The roles a node may have; the card colours each.
ROLE_SOLAR: Final = "solar"
ROLE_BATTERY: Final = "battery"
ROLE_GRID: Final = "grid"
ROLE_HOUSE: Final = "house"
ROLE_CAR: Final = "car"
ROLE_HEAT: Final = "heat"
SIDE_SOURCE: Final = "source"
SIDE_USE: Final = "use"

NODE_KEYS: Final = ("id", "side", "role", "kwh", "soc_pct")
LINK_KEYS: Final = ("from", "to", "kwh")
FLOW_KEYS: Final = (
    "date",
    "through",
    "hours",
    "car_hours",
    "heat_hours",
    "heat_sources",
    "nodes",
    "links",
    "step_min",
    "inverter_loss_kwh",
)

#: Each source's uses in the order it fills them (see the module); the sources are taken in this order. A car that
#: gives energy back (V2H) would be a source here after the battery: `(ROLE_CAR, (ROLE_GRID, ROLE_HOUSE))`.
ALLOCATION: Final = (
    (ROLE_SOLAR, (ROLE_HOUSE, ROLE_HEAT, ROLE_CAR, ROLE_BATTERY, ROLE_GRID)),
    (ROLE_BATTERY, (ROLE_GRID, ROLE_HOUSE, ROLE_HEAT, ROLE_CAR)),
    (ROLE_GRID, (ROLE_BATTERY, ROLE_HOUSE, ROLE_HEAT, ROLE_CAR)),
)
#: The uses, in the order the card stacks them; the order the house's shortfall is taken off them (see the module).
USES: Final = (ROLE_BATTERY, ROLE_HOUSE, ROLE_HEAT, ROLE_CAR, ROLE_GRID)
SHORTFALL_ORDER: Final = (ROLE_GRID, ROLE_BATTERY, ROLE_CAR, ROLE_HEAT)
#: Smaller flows (kWh) are left out of `links`.
MIN_LINK_KWH: Final = 0.005
#: The recorder's short-term statistics period, and the quarter hour the flow is allocated by.
FIVE_MIN: Final = timedelta(minutes=5)
QUARTER: Final = timedelta(minutes=15)
#: A quarter that ended less than this long ago may still miss its last 5-minute row (the recorder compiles each a few
#: seconds after its end): it is taken as running, with the rows it has.
SETTLE: Final = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class EnergyHour:
    """One period (kWh): a completed local hour, a quarter hour, or the running quarter's finished 5-minute rows
    (`minutes`). `solar` / `battery_*` are `None` on a site without that statistic; `car` is `None` when the cars'
    energy could not be read for the period (it is then in the house), and so is `heat` (the heat pump's meter)
    without its figure. `inverter_loss`: what turning the battery to the AC side (`on_ac_side`) took out of the house,
    `None` where it is not turned."""

    start: datetime
    grid_in: float
    grid_out: float
    solar: float | None = None
    battery_in: float | None = None
    battery_out: float | None = None
    car: float | None = None
    heat: float | None = None
    minutes: int = 60
    inverter_loss: float | None = None

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)


def with_heat_parts(periods: Sequence[EnergyHour], parts: Sequence[Mapping[datetime, float]]) -> list[EnergyHour]:
    """The periods with each part's energy (kWh per UTC quarter-hour start) added to their heat: an hour takes its
    four quarters, a quarter hour its own (the running one what is counted so far). A period with nothing of them
    keeps its heat as it was (`None` stays `None`)."""
    if not parts:
        return list(periods)
    out: list[EnergyHour] = []
    for period in periods:
        count = 4 if period.minutes >= 60 else -(-period.minutes // 15)
        added = sum(part.get(period.start + QUARTER * index, 0.0) for part in parts for index in range(count))
        if added <= 0:
            out.append(period)
            continue
        out.append(
            EnergyHour(
                start=period.start,
                grid_in=period.grid_in,
                grid_out=period.grid_out,
                solar=period.solar,
                battery_in=period.battery_in,
                battery_out=period.battery_out,
                car=period.car,
                heat=(period.heat or 0.0) + added,
                minutes=period.minutes,
                inverter_loss=period.inverter_loss,
            )
        )
    return out


def on_ac_side(periods: Sequence[EnergyHour], loss: tuple[float, float]) -> list[EnergyHour]:
    """The periods with the battery's charge and discharge turned from the battery's own counters (DC) to the AC side
    with the inverter's `(charge, discharge)` loss (see the module): of a charge, what the period's import can have
    given (with its loss, at most the import) at the way-in loss and the rest at the way-out loss; a discharge at the
    way-out loss. Each period keeps what that took out of the house (`inverter_loss`); one without the battery's
    figures is kept as it is."""
    charge, discharge = loss
    out: list[EnergyHour] = []
    for period in periods:
        if period.battery_in is None or period.battery_out is None:
            out.append(period)
            continue
        charged = max(0.0, period.battery_in)
        given = max(0.0, period.battery_out)
        from_grid = min(charged, max(0.0, period.grid_in) / (1.0 + charge))
        charged_ac = from_grid * (1.0 + charge) + (charged - from_grid) * (1.0 - discharge)
        given_ac = given * (1.0 - discharge)
        out.append(
            replace(
                period,
                battery_in=charged_ac,
                battery_out=given_ac,
                inverter_loss=(charged_ac - charged) + (given - given_ac),
            )
        )
    return out


def node_id(side: str, role: str) -> str:
    return f"{side}.{role}"


def allocate_hour(hour: EnergyHour) -> dict[tuple[str, str], float]:
    """The hour's flows (kWh) by `(source role, use role)`, by the module's rule."""
    sources = {
        ROLE_SOLAR: max(0.0, hour.solar or 0.0),
        ROLE_BATTERY: max(0.0, hour.battery_out or 0.0),
        ROLE_GRID: max(0.0, hour.grid_in),
    }
    uses = {
        ROLE_BATTERY: max(0.0, hour.battery_in or 0.0),
        ROLE_CAR: max(0.0, hour.car or 0.0),
        ROLE_HEAT: max(0.0, hour.heat or 0.0),
        ROLE_GRID: max(0.0, hour.grid_out),
    }
    house = sum(sources.values()) - sum(uses.values())
    if house < 0:
        shortfall = -house
        for role in SHORTFALL_ORDER:
            taken = min(shortfall, uses[role])
            uses[role] -= taken
            shortfall -= taken
        house = 0.0
    uses[ROLE_HOUSE] = house
    flows: dict[tuple[str, str], float] = {}
    for source, order in ALLOCATION:
        for use in order:
            amount = min(sources[source], uses[use])
            if amount <= 0:
                continue
            flows[(source, use)] = flows.get((source, use), 0.0) + amount
            sources[source] -= amount
            uses[use] -= amount
    # Step 4: what is left is netted out (see the module).
    return flows


def energy_flow(
    hours: Sequence[EnergyHour],
    *,
    day: date,
    has_solar: bool,
    has_battery: bool,
    soc_pct: float | None = None,
    has_heat: bool = False,
    heat_sources: int = 1,
    step_min: int = 60,
    since: datetime | None = None,
) -> dict[str, Any] | None:
    """The `energy_flow` block (`FLOW_KEYS`) for the day's periods; without any, `None`, or the empty flow from
    `since` on (today's first minutes, see the module). `inverter_loss_kwh` is the periods' `inverter_loss` summed
    where any of them has one (never below 0), else `None`."""
    if not hours and since is None:
        return None
    totals: dict[tuple[str, str], float] = {}
    for hour in hours:
        for key, kwh in allocate_hour(hour).items():
            totals[key] = totals.get(key, 0.0) + kwh
    source_roles = [ROLE_SOLAR] if has_solar else []
    source_roles += [ROLE_BATTERY] if has_battery else []
    source_roles.append(ROLE_GRID)
    use_roles = [role for role in USES if (role != ROLE_BATTERY or has_battery) and (role != ROLE_HEAT or has_heat)]
    nodes: list[dict[str, Any]] = []
    for side, roles in ((SIDE_SOURCE, source_roles), (SIDE_USE, use_roles)):
        for role in roles:
            kwh = sum(v for (src, use), v in totals.items() if (src if side == SIDE_SOURCE else use) == role)
            nodes.append(
                {
                    "id": node_id(side, role),
                    "side": side,
                    "role": role,
                    "kwh": round(kwh, 2),
                    "soc_pct": None
                    if soc_pct is None or side != SIDE_USE or role != ROLE_BATTERY
                    else round(soc_pct, 1),
                }
            )
    links = [
        {"from": node_id(SIDE_SOURCE, src), "to": node_id(SIDE_USE, use), "kwh": round(kwh, 2)}
        for src, _order in ALLOCATION
        for use in USES
        if (kwh := totals.get((src, use), 0.0)) >= MIN_LINK_KWH
    ]
    through = max((hour.end for hour in hours), default=since)
    assert through is not None
    return {
        "date": day.isoformat(),
        "through": through.isoformat(),
        "hours": len(hours),
        "car_hours": sum(1 for hour in hours if hour.car is not None),
        "heat_hours": sum(1 for hour in hours if hour.heat is not None),
        "heat_sources": max(1, heat_sources) if has_heat else 0,
        "nodes": nodes,
        "links": links,
        "step_min": step_min,
        "inverter_loss_kwh": inverter_loss_kwh(hours),
    }


def inverter_loss_kwh(periods: Sequence[EnergyHour]) -> float | None:
    """What turning the battery to the AC side took out of the house over the periods (kWh, see the module)."""
    losses = [period.inverter_loss for period in periods if period.inverter_loss is not None]
    return round(max(0.0, sum(losses)), 2) if losses else None


def energy_hours(
    starts: Sequence[datetime],
    grid_in: Mapping[datetime, float],
    grid_out: Mapping[datetime, float] | None,
    solar: Mapping[datetime, float] | None,
    battery_in: Mapping[datetime, float] | None,
    battery_out: Mapping[datetime, float] | None,
    car: Mapping[datetime, float] | None,
    heat: Mapping[datetime, float] | None = None,
) -> list[EnergyHour]:
    """The hours of `starts` every needed statistic has (UTC hour starts): the grid's import always, the export, the
    sun and the battery where the site has them (`None`: it has not; a site without an export meter exports nothing);
    an hour a needed statistic lacks is left out. The cars' figure is the hour's where `car` has it, and heat's where
    `heat` has it."""
    out: list[EnergyHour] = []
    for start in starts:
        if start not in grid_in:
            continue
        if any(series is not None and start not in series for series in (grid_out, solar, battery_in, battery_out)):
            continue
        out.append(
            EnergyHour(
                start=start,
                grid_in=grid_in[start],
                grid_out=0.0 if grid_out is None else grid_out[start],
                solar=None if solar is None else solar[start],
                battery_in=None if battery_in is None else battery_in[start],
                battery_out=None if battery_out is None else battery_out[start],
                car=None if car is None else car.get(start),
                heat=None if heat is None else heat.get(start),
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class FlowSeries:
    """The energy (kWh) per period start of each meter the flow reads (`None`: the site has no such meter; `car`
    and `heat` may lack a period). For `quarter_hours` the periods are the recorder's 5-minute rows, except
    `car_quarters`: the battery plan's measure of the cars per quarter hour (kWh over the whole quarter), used where
    the chargers have no register."""

    grid_in: Mapping[datetime, float]
    grid_out: Mapping[datetime, float] | None = None
    solar: Mapping[datetime, float] | None = None
    battery_in: Mapping[datetime, float] | None = None
    battery_out: Mapping[datetime, float] | None = None
    car: Mapping[datetime, float] | None = None
    heat: Mapping[datetime, float] | None = None
    car_quarters: Mapping[datetime, float] | None = None

    def needed(self) -> list[Mapping[datetime, float]]:
        return [
            s for s in (self.grid_in, self.grid_out, self.solar, self.battery_in, self.battery_out) if s is not None
        ]


def _sum(series: Mapping[datetime, float] | None, slots: Sequence[datetime]) -> float | None:
    if series is None or any(slot not in series for slot in slots):
        return None
    return sum(series[slot] for slot in slots)


def quarter_hours(
    starts: Sequence[datetime], five: FlowSeries, now: datetime
) -> tuple[list[EnergyHour], list[datetime]]:
    """The quarter hours of the hours `starts` (UTC hour starts) from 5-minute rows, and the finished hours that could
    not be built whole (some quarter lacks a row of a needed meter: read them by the hour instead).

    A quarter that ended (`SETTLE` before `now`) needs its three rows from every needed meter; the running quarter
    (and one just ended) is the run of its first rows every needed meter has, `minutes` long, and ends the day's
    quarters (none of it yet: nothing). An open hour's gap ends its quarters too. The cars' and heat's figure is the
    sum of the same rows where their series has them all, else `None`; the battery plan's quarter (`car_quarters`)
    counts its share of the quarter's minutes."""
    settled = now - SETTLE
    needed = five.needed()
    out: list[EnergyHour] = []
    missing: list[datetime] = []
    for hour in starts:
        built: list[EnergyHour] = []
        whole = True
        running = False
        for index in range(4):
            quarter = hour + index * QUARTER
            if quarter >= now:
                running = True
                break
            slots = [quarter + k * FIVE_MIN for k in range(3)]
            if quarter + QUARTER > settled:
                running = True
                taken = []
                for slot in slots:
                    if slot + FIVE_MIN > now or not all(slot in series for series in needed):
                        break
                    taken.append(slot)
                slots = taken
                if not slots:
                    break
            elif not all(slot in series for series in needed for slot in slots):
                whole = False
                break
            car = _sum(five.car, slots)
            if car is None and not five.car and five.car_quarters is not None and quarter in five.car_quarters:
                car = five.car_quarters[quarter] * len(slots) / 3
            built.append(
                EnergyHour(
                    start=quarter,
                    minutes=5 * len(slots),
                    grid_in=_sum(five.grid_in, slots) or 0.0,
                    grid_out=_sum(five.grid_out, slots) or 0.0,
                    solar=_sum(five.solar, slots),
                    battery_in=_sum(five.battery_in, slots),
                    battery_out=_sum(five.battery_out, slots),
                    car=car,
                    heat=_sum(five.heat, slots),
                )
            )
            if running:
                break
        if whole:
            out.extend(built)
        elif hour + timedelta(hours=1) <= settled:
            missing.append(hour)
        else:
            # The open hour: its quarters up to the gap.
            out.extend(built)
        if running:
            break
    return out, missing


def hour_figures(periods: Sequence[EnergyHour], field: str, tz_hour: Any) -> dict[datetime, float]:
    """A figure (`car` or `heat`) per hour start (`tz_hour(moment)` gives it) from the periods that cover the hour
    whole, each with the figure; hours partly covered or without it are left out."""
    sums: dict[datetime, float] = {}
    minutes: dict[datetime, int] = {}
    unread: set[datetime] = set()
    for period in periods:
        start = tz_hour(period.start)
        value = getattr(period, field)
        if value is None:
            unread.add(start)
            continue
        sums[start] = sums.get(start, 0.0) + value
        minutes[start] = minutes.get(start, 0) + period.minutes
    return {start: kwh for start, kwh in sums.items() if minutes[start] == 60 and start not in unread}
