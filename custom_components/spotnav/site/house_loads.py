"""`spotnav/get_house_loads`'s answer (API v1): what the house's known meters used of the house's energy over a day,
month or year, for the pie behind "House" in the house card's energy flow (SpotNav 3.0, shadow); and, with
`kind: "heat"`, what the heat's sources used of the heat, for the pie behind "Heat".

Pure: the shell (`api/house_loads.py`) reads the Energy dashboard's individual devices and their statistics and hands
them over with the house's (or the heat's) figure from the energy flow; nothing is read or written here.

    {api_version: 1, site_id, kind: "house" | "heat", period: "day" | "month" | "year", date (the period's first
     day), end (the first day after it), through (the end of the energy flow's last period) | null,
     house_kwh (the block's figure: the house, or the heat) | null, meters_kwh, other_kwh | null, over_kwh,
     loads: [{statistic_id, name, kwh, source: "meter" | "heat_pump" | "zone" | "pool_heat" | "pool_circulation" | "spa"
              | "sauna", changed}],
     moved: [{statistic_id, name, belongs}], belongs: {statistic_id: "heat" | "car" | "none"},
     implausible: [{statistic_id, name, hours, left_out}]}

* **The meters** are the Energy dashboard's individual devices (`energy/get_prefs` → `device_consumption`), found
  with no setting. Sorted out by themselves (`counted_meters`), each counted only where it belongs: a device whose
  energy or power sensor is one of the site's chargers' (its energy register, its power sensor or its charge
  control), or that is on the charger's own device: the car is a block of its own in the flow; the heat pump's meter
  SpotNav reads (`site/heat_meter.py`, by its sensor alone: a multi-channel clamp meter's other circuits share its
  device and stay in the house) and a heat zone's sensor or a device on a heat zone's own device
  (`site/heat_zones.py`): they are in the heat; and a device the dashboard nests inside another
  (`included_in_stat`), which its parent already holds.
* **Where a meter belongs** (`belongs`, the site's `meter_belongs`, written by `spotnav/update_site_settings`): a
  meter SpotNav cannot tie to a load by itself (a Shelly in the fuse box on the floor heating's circuit) is the
  house's until the household says otherwise in the pie: `heat` (counted in the heat, its flow block and its pie,
  `site/heat_zones.py`), `car` (already counted with the car: the car's figure is the chargers' own, so the meter is
  only taken out of the house pie) or `none` (counted nowhere: a meter inside another). A meter moved out of the
  house is listed in the house pie's `moved` (to bring it back); in the heat pie a moved meter is a row with
  `source` `meter` and `changed` true.
* **The heat pie** (`kind: "heat"`): the heat pump's meter (`heat_pump`), each heat zone counted (`zone`, named by
  its thermostat), each meter moved to heat (`meter`) and the pool's heat pump and circulation, the spa and the sauna
  (`pool_heat`, `pool_circulation`, `spa`, `sauna`, named by their entity; `statistic_id` their entity: the energy
  the pool and spa shadow counted, as the energy flow's heat holds it, `api/house.py`), a part whose device the meter
  or a zone already reads left out; a dashboard meter on such a part's device is sorted out of the house pie as a
  heat zone's is; `house_kwh` is the flow's heat block and `other_kwh` what
  the flow's heat holds beyond them (none, but for rounding and hours the meter missed).
* **The figures** (`meter_kwh`): each meter's energy over the same span as the flow's house block (the period's
  start up to `through`): the recorder's hourly statistics where the hour is whole, else its 5-minute ones (today's
  running hour). `loads` is sorted by energy, largest first.
* **Hours that cannot be** (`plausible_kwh`): a meter's hour far above what the whole site used in that hour (the
  house, the heat and the car together: a dashboard device that also measures the car or the heat stays counted, see
  `over_kwh`; more than `PLAUSIBLE_FACTOR` times it and more than `PLAUSIBLE_MARGIN_KWH` over it, so a meter that
  lags into the next hour stays counted too), or above the main fuse's capacity for an hour, is a broken statistic (a
  plug whose total drops and comes back: the recorder counts every return as new energy), not energy: it is not
  counted. Each meter that lost hours so is in
  `implausible` (`{statistic_id, name, hours, left_out}`), so the card says so once and points to Home Assistant's
  statistics repair (an actionable line); a meter with no plausible hour left is left out of the pie (`left_out`).
  The same rule holds for the heat pie's sources.
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

#: Where a meter belongs (`meter_belongs`, see the module): the house (the default, never stored), heat, the car, or
#: nowhere (counted nowhere: a meter inside another).
BELONG_HOUSE: Final = "house"
BELONG_HEAT: Final = "heat"
BELONG_CAR: Final = "car"
BELONG_NONE: Final = "none"
BELONGS: Final = (BELONG_HOUSE, BELONG_HEAT, BELONG_CAR, BELONG_NONE)
#: The most meters a site keeps a choice for.
MAX_BELONGS: Final = 64
LOADS_KEYS: Final = (
    "api_version",
    "site_id",
    "kind",
    "period",
    "date",
    "end",
    "through",
    "house_kwh",
    "meters_kwh",
    "other_kwh",
    "over_kwh",
    "loads",
    "moved",
    "belongs",
    "implausible",
)
IMPLAUSIBLE_KEYS: Final = ("statistic_id", "name", "hours", "left_out")
#: An hour's meter figure is a broken statistic only when it is more than this many times the site's whole use in
#: the hour and more than `PLAUSIBLE_MARGIN_KWH` over it (a meter that lags into the next hour, or one that also
#: measures the car or the heat, stays counted).
PLAUSIBLE_FACTOR: Final = 2.0
PLAUSIBLE_MARGIN_KWH: Final = 1.0
LOAD_KEYS: Final = ("statistic_id", "name", "kwh", "source", "changed")
MOVED_KEYS: Final = ("statistic_id", "name", "belongs")
KIND_HOUSE: Final = "house"
KIND_HEAT: Final = "heat"
SOURCE_METER: Final = "meter"
SOURCE_HEAT_PUMP: Final = "heat_pump"
SOURCE_ZONE: Final = "zone"
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
    #: What the row is in a pie: an Energy dashboard device (`meter`), the heat pump's meter or a heat zone.
    source: str = SOURCE_METER


def counted_meters(
    meters: Iterable[DashboardMeter],
    *,
    car_entities: Iterable[str] = (),
    car_devices: Iterable[str] = (),
    heat_entities: Iterable[str] = (),
    heat_devices: Iterable[str] = (),
) -> list[DashboardMeter]:
    """The meters the pie counts: not a charger's (by its sensors or its device), not the heat's (the heat pump's
    meter or a heat zone's sensor, or on a heat zone's device), not one nested in another dashboard device, each
    statistic once."""
    meters = list(meters)
    cars = set(car_entities)
    devices = {device for device in car_devices if device}
    heat = set(heat_entities)
    heat_devs = {device for device in heat_devices if device}
    present = {meter.statistic_id for meter in meters}
    out: dict[str, DashboardMeter] = {}
    for meter in meters:
        sensors = {meter.statistic_id, *([meter.rate_id] if meter.rate_id else [])}
        if sensors & cars or (meter.device_id is not None and meter.device_id in devices):
            continue
        if sensors & heat or (meter.device_id is not None and meter.device_id in heat_devs):
            continue
        if meter.included_in is not None and meter.included_in in present:
            continue
        out.setdefault(meter.statistic_id, meter)
    return list(out.values())


def stored_belongs(data: Mapping[str, Any] | None, key: str = "meter_belongs") -> dict[str, str]:
    """The household's choices as stored (`CONF_METER_BELONGS`): only a statistic id with heat, car or none; anything
    else reads as the house (left out)."""
    raw = None if data is None else data.get(key)
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(stat): value
        for stat, value in raw.items()
        if isinstance(stat, str) and stat and value in (BELONG_HEAT, BELONG_CAR, BELONG_NONE)
    }


def merged_belongs(current: Mapping[str, str], changes: Any) -> dict[str, str] | None:
    """`current` with `changes` (`{statistic id: one of BELONGS}`; the house removes the choice) applied, or `None`
    when `changes` is not such a mapping or the result holds more than `MAX_BELONGS`."""
    if not isinstance(changes, Mapping) or not changes:
        return None
    out = dict(current)
    for stat, value in changes.items():
        if not isinstance(stat, str) or not stat or len(stat) > 255 or value not in BELONGS:
            return None
        if value == BELONG_HOUSE:
            out.pop(stat, None)
        else:
            out[stat] = value
    return out if len(out) <= MAX_BELONGS else None


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


def split_belongs(
    meters: Iterable[DashboardMeter], belongs: Mapping[str, str]
) -> tuple[list[DashboardMeter], list[tuple[DashboardMeter, str]]]:
    """The meters the house pie shows (the house's) and those the household moved out of it, with where to."""
    shown: list[DashboardMeter] = []
    moved: list[tuple[DashboardMeter, str]] = []
    for meter in meters:
        where = belongs.get(meter.statistic_id, BELONG_HOUSE)
        if where == BELONG_HOUSE:
            shown.append(meter)
        else:
            moved.append((meter, where))
    return shown, moved


def house_loads(
    *,
    site_id: str,
    period: str,
    first: str,
    end: str,
    through: datetime | None,
    house_kwh: float | None,
    loads: Iterable[tuple[DashboardMeter, float]],
    kind: str = KIND_HOUSE,
    moved: Iterable[tuple[DashboardMeter, str]] = (),
    belongs: Mapping[str, str] | None = None,
    implausible: Iterable[tuple[DashboardMeter, MeterRead]] = (),
) -> dict[str, Any]:
    """The whole answer (`LOADS_KEYS`). A meter of `implausible` with no plausible hour is left out of `loads`."""
    belongs = dict(belongs or {})
    flagged = [(meter, read) for meter, read in implausible if read.dropped > 0]
    gone = {meter.statistic_id for meter, read in flagged if read.left_out}
    loads = [(meter, kwh) for meter, kwh in loads if meter.statistic_id not in gone]
    rows = sorted(
        (
            {
                "statistic_id": meter.statistic_id,
                "name": meter.name,
                "kwh": round(max(0.0, kwh), 3),
                "source": meter.source,
                "changed": meter.source == SOURCE_METER and meter.statistic_id in belongs,
            }
            for meter, kwh in loads
        ),
        key=lambda row: (-row["kwh"], row["name"].casefold()),
    )
    meters = round(sum(row["kwh"] for row in rows), 3)
    other = None if house_kwh is None else round(max(0.0, house_kwh - meters), 3)
    over = 0.0 if house_kwh is None else meters - house_kwh
    return {
        "api_version": HOUSE_LOADS_API_VERSION,
        "site_id": site_id,
        "kind": kind,
        "period": period,
        "date": first,
        "end": end,
        "through": None if through is None else through.isoformat(),
        "house_kwh": None if house_kwh is None else round(house_kwh, 3),
        "meters_kwh": meters,
        "other_kwh": other,
        "over_kwh": round(over, 3) if over > OVER_TOLERANCE_KWH else 0.0,
        "loads": rows,
        "moved": sorted(
            (
                {"statistic_id": meter.statistic_id, "name": meter.name, "belongs": where}
                for meter, where in moved
            ),
            key=lambda row: row["name"].casefold(),
        ),
        "belongs": dict(sorted(belongs.items())),
        "implausible": [
            {"statistic_id": meter.statistic_id, "name": meter.name, "hours": read.dropped, "left_out": read.left_out}
            for meter, read in sorted(flagged, key=lambda item: item[0].name.casefold())
        ],
    }


@dataclass(frozen=True, slots=True)
class MeterRead:
    """A meter's energy over a span from its plausible hours, how many hours it had a figure for and how many of
    them were dropped as implausible."""

    kwh: float
    hours: int = 0
    dropped: int = 0

    @property
    def left_out(self) -> bool:
        return self.dropped > 0 and self.dropped >= self.hours


def _hour_of(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def plausible_kwh(
    hourly: Mapping[datetime, float],
    five: Mapping[datetime, float],
    start: datetime,
    through: datetime,
    *,
    limits: Mapping[datetime, float] | None = None,
    cap_kwh: float | None = None,
) -> MeterRead:
    """A meter's energy in `[start, through)` by `period_total`'s rule, each hour's figure (the hourly one, else its
    5-minute ones in the span) dropped when it is grossly above `limits[hour]` (the site's whole use in that UTC hour:
    more than `PLAUSIBLE_FACTOR` times it and more than `PLAUSIBLE_MARGIN_KWH` over it, see the module), or above
    `cap_kwh` (the main fuse's hour)."""
    by_hour: dict[datetime, float] = {}
    for moment, kwh in hourly.items():
        if start <= moment and moment + _HOUR <= through:
            by_hour[_hour_of(moment)] = max(0.0, kwh)
    partial: dict[datetime, float] = {}
    for moment, kwh in five.items():
        hour = _hour_of(moment)
        if start <= moment < through and hour not in by_hour:
            partial[hour] = partial.get(hour, 0.0) + max(0.0, kwh)
    by_hour.update(partial)
    total = 0.0
    dropped = 0
    for hour, kwh in by_hour.items():
        limit = None if limits is None else limits.get(hour)
        gross = limit is not None and kwh > PLAUSIBLE_FACTOR * limit and kwh > limit + PLAUSIBLE_MARGIN_KWH
        if gross or (cap_kwh is not None and kwh > cap_kwh):
            dropped += 1
            continue
        total += kwh
    return MeterRead(total, len(by_hour), dropped)


def changes(rows: Sequence[Mapping[str, Any]]) -> dict[datetime, float]:
    """A statistic's rows as kWh per period start (their `change`)."""
    return _changes(rows)


def period_total(
    hourly: Mapping[datetime, float], five: Mapping[datetime, float], start: datetime, through: datetime
) -> float:
    """`meter_kwh`'s rule on figures already read (kWh per period start): each whole hour's hourly figure, the rest
    from the 5-minute ones inside the span."""
    used: set[datetime] = set()
    total = 0.0
    for moment, kwh in hourly.items():
        if start <= moment and moment + _HOUR <= through:
            total += max(0.0, kwh)
            used.add(moment)
    for moment, kwh in five.items():
        if start <= moment < through and moment.replace(minute=0, second=0, microsecond=0) not in used:
            total += max(0.0, kwh)
    return total
