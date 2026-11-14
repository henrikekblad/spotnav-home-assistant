"""Today's energy flow for the house card's Sankey diagram (SpotNav 3.0 preview, shadow): sources on one side, uses
on the other, and how much went from each source to each use.

Pure: the shell (`api/house.py`) reads the hours (`EnergyHour`) and hands them over; nothing is read or written here.

**What is measured** per completed local hour of today: the sun's production, the grid's import and export and the
home battery's charge and discharge, each from the Energy dashboard's hourly statistics (`site/energy_statistics.py`),
and the cars' energy (the site's chargers' energy registers, else what the battery plan measured in the hour). The
house is the balance: `sun + import + discharge - export - charge - cars`, never below 0 (when the meters disagree
and it would be, the missing energy is taken off the uses in the order export, battery charge, cars).

**What is not measured** is which source fed which use: no meter says whether the sun or the grid charged the
battery. The flows are allocated by one rule, hour by hour so a sunny midday hour and a night of grid charging do not
mix:

1. **The sun** goes to the house, then the cars, then the battery, then the grid (export).
2. **The battery's discharge** goes first to whatever export the sun left (the grid never exports, so the rest of the
   export can only be the battery's), then to the house, then the cars.
3. **The grid's import** goes first to whatever battery charge the sun left (the battery does not charge itself), then
   to the house, then the cars.
4. Whatever is left can only be a battery that both charged and discharged in the hour, or a grid that both imported
   and exported: it is netted out (left out of both sides), so the diagram never shows the battery feeding itself.

So every node balances: a source's figure is the sum of its flows, and so is a use's.

**Shape** (`energy_flow`): `nodes` and `links` are lists with a `role` and a `side`, not fixed fields, so a source or a
use can be added without a contract break. A node is left out when the site has nothing to measure it with (no solar
statistics: no sun node; no battery statistics: neither battery node); a use with no energy today stays in with 0.
A bidirectional charger (V2H/V2G, Sigenergy's DC charger) would add the car as a source: `role` `car` on the `source`
side, measured by the charger's export register into `EnergyHour.car_out`, allocated after the battery by the
battery's rule (export first, then the house); `ALLOCATION` holds the order and is where it would go.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final

#: The roles a node may have; the card colours each.
ROLE_SOLAR: Final = "solar"
ROLE_BATTERY: Final = "battery"
ROLE_GRID: Final = "grid"
ROLE_HOUSE: Final = "house"
ROLE_CAR: Final = "car"
SIDE_SOURCE: Final = "source"
SIDE_USE: Final = "use"

NODE_KEYS: Final = ("id", "side", "role", "kwh", "soc_pct")
LINK_KEYS: Final = ("from", "to", "kwh")
FLOW_KEYS: Final = ("date", "through", "hours", "car_hours", "nodes", "links")

#: Each source's uses in the order it fills them (see the module); the sources are taken in this order. A car that
#: gives energy back (V2H) would be a source here after the battery: `(ROLE_CAR, (ROLE_GRID, ROLE_HOUSE))`.
ALLOCATION: Final = (
    (ROLE_SOLAR, (ROLE_HOUSE, ROLE_CAR, ROLE_BATTERY, ROLE_GRID)),
    (ROLE_BATTERY, (ROLE_GRID, ROLE_HOUSE, ROLE_CAR)),
    (ROLE_GRID, (ROLE_BATTERY, ROLE_HOUSE, ROLE_CAR)),
)
#: The uses, in the order the card stacks them; the order the house's shortfall is taken off them (see the module).
USES: Final = (ROLE_BATTERY, ROLE_HOUSE, ROLE_CAR, ROLE_GRID)
SHORTFALL_ORDER: Final = (ROLE_GRID, ROLE_BATTERY, ROLE_CAR)
#: Smaller flows (kWh) are left out of `links`.
MIN_LINK_KWH: Final = 0.005


@dataclass(frozen=True, slots=True)
class EnergyHour:
    """One completed local hour (kWh). `solar` / `battery_*` are `None` on a site without that statistic; `car` is
    `None` when the cars' energy could not be read for the hour (it is then in the house)."""

    start: datetime
    grid_in: float
    grid_out: float
    solar: float | None = None
    battery_in: float | None = None
    battery_out: float | None = None
    car: float | None = None


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
) -> dict[str, Any] | None:
    """The `energy_flow` block (`FLOW_KEYS`) for today's completed hours; `None` without any."""
    if not hours:
        return None
    totals: dict[tuple[str, str], float] = {}
    for hour in hours:
        for key, kwh in allocate_hour(hour).items():
            totals[key] = totals.get(key, 0.0) + kwh
    source_roles = [ROLE_SOLAR] if has_solar else []
    source_roles += [ROLE_BATTERY] if has_battery else []
    source_roles.append(ROLE_GRID)
    use_roles = [role for role in USES if role != ROLE_BATTERY or has_battery]
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
    last = max(hour.start for hour in hours)
    return {
        "date": day.isoformat(),
        "through": (last + timedelta(hours=1)).isoformat(),
        "hours": len(hours),
        "car_hours": sum(1 for hour in hours if hour.car is not None),
        "nodes": nodes,
        "links": links,
    }


def energy_hours(
    starts: Sequence[datetime],
    grid_in: Mapping[datetime, float],
    grid_out: Mapping[datetime, float] | None,
    solar: Mapping[datetime, float] | None,
    battery_in: Mapping[datetime, float] | None,
    battery_out: Mapping[datetime, float] | None,
    car: Mapping[datetime, float] | None,
) -> list[EnergyHour]:
    """The hours of `starts` every needed statistic has (UTC hour starts): the grid's import always, the export, the
    sun and the battery where the site has them (`None`: it has not; a site without an export meter exports nothing);
    an hour a needed statistic lacks is left out. The cars' figure is the hour's where `car` has it."""
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
            )
        )
    return out
