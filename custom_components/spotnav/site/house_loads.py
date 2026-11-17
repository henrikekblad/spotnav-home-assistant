"""`spotnav/get_house_loads`'s answer (API v1): what the house's known meters used of the house's energy over a day,
month or year, for the pie behind "House" in the house card's energy flow (SpotNav 3.0, shadow).

Pure: the shell (`api/house_loads.py`) reads the Energy dashboard's individual devices and their statistics and hands
them over with the house's figure from the energy flow; nothing is read or written here.

    {api_version: 1, site_id, period: "day" | "month" | "year", date (the period's first day), end (the first day
     after it), through (the end of the energy flow's last period) | null, house_kwh | null, meters_kwh,
     other_kwh | null, over_kwh, loads: [{statistic_id, name, kwh}]}

* **The meters** are the Energy dashboard's individual devices (`energy/get_prefs` → `device_consumption`), found
  with no setting. Left out (`counted_meters`): a device whose energy or power sensor is one of the site's chargers'
  (its energy register, its power sensor or its charge control, or any sensor on the charger's own device): the car
  is a block of its own in the flow; the heat pump's meter SpotNav reads (`site/heat_meter.py`): so is the heat; and
  a device the dashboard nests inside another (`included_in_stat`), which its parent already holds.
* **The figures** (`meter_kwh`): each meter's energy over the same span as the flow's house block (the period's
  start up to `through`): the recorder's hourly statistics where the hour is whole, else its 5-minute ones (today's
  running hour). `loads` is sorted by energy, largest first.
* **Övrigt** (`other_kwh`): the house block less the meters, never below 0; `over_kwh` is how much the meters
  exceed the house (a device meter that also measures the car or the heat, or meters that disagree with the
  grid's), 0 when they do not. `house_kwh` and `other_kwh` are `null` without an energy flow (no grid meter on the dashboard).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Final

HOUSE_LOADS_API_VERSION: Final = 1
LOADS_KEYS: Final = (
    "api_version",
    "site_id",
    "period",
    "date",
    "end",
    "through",
    "house_kwh",
    "meters_kwh",
    "other_kwh",
    "over_kwh",
    "loads",
)
LOAD_KEYS: Final = ("statistic_id", "name", "kwh")
#: Smaller differences (kWh) between the meters and the house are rounding, not a mismatch.
OVER_TOLERANCE_KWH: Final = 0.01
_HOUR: Final = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class DashboardMeter:
    """One of the Energy dashboard's individual devices: its energy statistic, its power sensor (`None` without), the
    name it is shown by, the Home Assistant device its statistic's entity is on and the statistic it is nested in."""

    statistic_id: str
    name: str
    rate_id: str | None = None
    device_id: str | None = None
    included_in: str | None = None


def counted_meters(
    meters: Iterable[DashboardMeter],
    *,
    car_entities: Iterable[str] = (),
    car_devices: Iterable[str] = (),
    heat_entities: Iterable[str] = (),
) -> list[DashboardMeter]:
    """The meters the pie counts: not a charger's (by its sensors or its device), not the heat pump's meter, not one
    nested in another dashboard device, each statistic once."""
    meters = list(meters)
    cars = set(car_entities)
    devices = {device for device in car_devices if device}
    heat = set(heat_entities)
    present = {meter.statistic_id for meter in meters}
    out: dict[str, DashboardMeter] = {}
    for meter in meters:
        sensors = {meter.statistic_id, *([meter.rate_id] if meter.rate_id else [])}
        if sensors & cars or (meter.device_id is not None and meter.device_id in devices):
            continue
        if sensors & heat:
            continue
        if meter.included_in is not None and meter.included_in in present:
            continue
        out.setdefault(meter.statistic_id, meter)
    return list(out.values())


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _changes(rows: Sequence[Mapping[str, Any]]) -> dict[datetime, float]:
    out: dict[datetime, float] = {}
    for row in rows:
        start, change = row.get("start"), row.get("change")
        if not _finite(change):
            continue
        if isinstance(start, datetime):
            moment = start.astimezone(timezone.utc)
        elif _finite(start):
            moment = datetime.fromtimestamp(float(start), timezone.utc)
        else:
            continue
        out[moment] = max(0.0, float(change))
    return out


def meter_kwh(
    hour_rows: Sequence[Mapping[str, Any]], five_rows: Sequence[Mapping[str, Any]], start: datetime, through: datetime
) -> float:
    """One meter's energy in `[start, through)`: each whole hour's hourly row, the rest (an hour with no hourly row
    yet, a running hour) from the 5-minute rows inside the span."""
    hourly = _changes(hour_rows)
    used: set[datetime] = set()
    total = 0.0
    for moment, kwh in hourly.items():
        if start <= moment and moment + _HOUR <= through:
            total += kwh
            used.add(moment)
    for moment, kwh in _changes(five_rows).items():
        if start <= moment < through and moment.replace(minute=0, second=0, microsecond=0) not in used:
            total += kwh
    return total


def house_loads(
    *,
    site_id: str,
    period: str,
    first: str,
    end: str,
    through: datetime | None,
    house_kwh: float | None,
    loads: Iterable[tuple[DashboardMeter, float]],
) -> dict[str, Any]:
    """The whole answer (`LOADS_KEYS`)."""
    rows = sorted(
        ({"statistic_id": meter.statistic_id, "name": meter.name, "kwh": round(max(0.0, kwh), 3)} for meter, kwh in loads),
        key=lambda row: (-row["kwh"], row["name"].casefold()),
    )
    meters = round(sum(row["kwh"] for row in rows), 3)
    other = None if house_kwh is None else round(max(0.0, house_kwh - meters), 3)
    over = 0.0 if house_kwh is None else meters - house_kwh
    return {
        "api_version": HOUSE_LOADS_API_VERSION,
        "site_id": site_id,
        "period": period,
        "date": first,
        "end": end,
        "through": None if through is None else through.isoformat(),
        "house_kwh": None if house_kwh is None else round(house_kwh, 3),
        "meters_kwh": meters,
        "other_kwh": other,
        "over_kwh": round(over, 3) if over > OVER_TOLERANCE_KWH else 0.0,
        "loads": rows,
    }
