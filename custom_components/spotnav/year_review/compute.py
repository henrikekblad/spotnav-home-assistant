"""The year's review, worked out (plan `plans/year_review_plan.md`, "Året med SpotNav"). Pure: the shell
(`year_review/runtime.py`) gathers the inputs and runs `compute_review` in the executor; nothing is read or written
here, so the card and the app get the same pages from one place.

**The year** (`review_year`, `year_bounds`): in December the year so far (1 January to yesterday), in January the year
before, whole. The review is worked out each night from 10 December to 31 December and once on 1 January (the year's
final one, kept through January); outside that only when the service asks (`spotnav.show_year_review`).

**The answer** (`spotnav/get_year_review`, `review_answer`):

    {api_version: 1, site_id, year, state: "ready" | "computing" | "none", banner, row, review | null}

and the review (`compute_review`), money in the area's minor unit (öre), dates ISO, numbers only, no entity id or name:

    {year, through, since | null, partial, money: {currency, major_unit, minor_unit} | null,
     pages: [{id: "cover"},
             {id: "saved", total, battery, car, heat, estimated, starts: {battery, car, heat}},
             {id: "car", cheap_share_pct, charges, kwh, cheapest: {price, date} | null, estimated, since},
             {id: "sun", kwh, home_pct, export_paid, since},
             {id: "battery", earned, earned_since, saved, saved_since, cycles, buy_price, use_price, kwh_in,
              kwh_out},
             {id: "fuse", count, since},
             {id: "expensive", price, start, bought_kwh, since},
             {id: "thanks"}]}

The prices are read from the year store's day parts (`DayParts.price`, `PriceDay`: worked out once per finished day
with that day's fees, `price_day_of`), never from the year's slots. A page left out of `pages` has nothing to stand
on. Each page's rule:

* **cover**: always. `since` is the first day of SpotNav's own record (the site's set-up, the first charge recorded
  live) where that is after 7 January, else `null`; `partial` with a `since`.
* **saved**: what SpotNav saved (`site/spotnav_saving.py`) summed over the year's day parts (kept per day by the year
  store, so a part whose source keeps fewer days is not lost): the battery's share SpotNav controlled, the car against
  charging at once on plug-in, the heat's review. Before 3.0 a charge SpotNav started (plan, sun or hybrid) with no
  plug-in time is counted at the day-average estimate (`ChargeSession.savings_minor`) and the page is `estimated`. A
  part is `null` where no day has it; `starts` names each part's first day where that is after 7 January. Left out
  when the total is under one major unit or fewer than `MIN_SAVED_DAYS` days have any part. Never a saving for a time
  SpotNav did not control.
* **car**: the sessions that started in the year (`charges`, `kwh`). `cheap_share_pct` is the share of the priced kWh
  drawn in the cheapest third of each local day's price intervals (by the buy price with that day's fees); `null`
  under `MIN_PRICED_CAR_KWH` priced kWh; `since` the first priced day where the priced kWh are under
  `PRICED_CAR_SHARE` of the year's. `cheapest` the session of at least `MIN_CHEAPEST_KWH` kWh, every kWh priced,
  with the lowest mean buy price (the session's own price slices, the charger card's figure). `estimated` where an
  imported (hourly) session is in it. Left out without a session.
* **sun**: the Energy dashboard's solar meter by the hour (`kwh`); `home_pct` the share the house, the battery and the
  cars used (the energy flow's rule, hour by hour), `null` without an export meter (nothing is guessed); `export_paid`
  whether what went out earned anything at the held sell prices (`null` without them), so the card says "sold" only
  where it was; `since` the first hour read where that is after 7 January. Left out without a solar meter.
* **battery**: the Energy dashboard's battery meters over the year (`kwh_in`, `kwh_out`; `cycles` = out over the
  battery's capacity, from the first day of the money shown (`earned_since` / `saved_since`) so both cover one span); the money (`earned`, the arbitrage the house card's headline shows, `battery/stored_value.py`;
  `buy_price` and `use_price`, what a charged kWh cost and a given one was worth) only from `MIN_BATTERY_DAYS` days of
  SpotNav's own count on, `earned_since` its first day where after 7 January. `saved` what the battery saved whoever
  controlled it (an inverter's own optimiser too), from the meters' hours and the held hourly prices
  (`battery_saved`): each priced hour's discharge at its buy price, less its charge, the part the grid's import
  covers (`min(battery_in, grid_in)`) at the buy price and the rest (the sun's) at the sell price it gave up; `null`
  under `MIN_BATTERY_DAYS` priced days with the battery's hours or under one major unit, `saved_since` the first
  priced day where after 7 January. Left out without a battery meter and without a counted day.
* **fuse**: the overload episodes the regulator lowered a charge for (counted once per episode since the counter
  started, `site/overload_episodes.py`). Left out under `MIN_FUSE_DAYS` counted days or `MIN_FUSE_COUNT` episodes.
* **expensive**: the clock hour with the highest mean buy price (spot with that day's fees) among the held prices
  of the year, its start (local, with its offset) and the grid's import in it (`bought_kwh`, `null` without the
  hour's row); `since` the first priced day where under `FULL_PRICE_DAYS` days are priced. Left out under
  `MIN_PRICE_DAYS` priced days.
* **thanks**: always last.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
import math
from typing import Any, Final

from .. import time_zones
from ..battery.plan_shadow import PriceSlot
from ..sessions.model import ChargeSession
from ..site.energy_flow import ROLE_GRID, ROLE_SOLAR, EnergyHour, allocate_hour
from ..site.spotnav_saving import PLANNED, DaySaving

YEAR_REVIEW_API_VERSION: Final = 1
ANSWER_KEYS: Final = ("api_version", "site_id", "year", "state", "banner", "row", "review")
REVIEW_KEYS: Final = ("year", "through", "since", "partial", "money", "pages")
PAGE_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "cover": ("id",),
    "saved": ("id", "total", "battery", "car", "heat", "estimated", "starts"),
    "car": ("id", "cheap_share_pct", "charges", "kwh", "cheapest", "estimated", "since"),
    "sun": ("id", "kwh", "home_pct", "export_paid", "since"),
    "battery": (
        "id", "earned", "earned_since", "saved", "saved_since", "cycles", "buy_price", "use_price", "kwh_in", "kwh_out"
    ),
    "fuse": ("id", "count", "since"),
    "expensive": ("id", "price", "start", "bought_kwh", "since"),
    "thanks": ("id",),
}
PAGE_ORDER: Final = tuple(PAGE_KEYS)

STATE_READY: Final = "ready"
STATE_COMPUTING: Final = "computing"
STATE_NONE: Final = "none"

#: The review is worked out each night from this day of December, and shown from it until the end of January.
WINDOW_DAY: Final = 10
#: A record that starts on or before this day of January counts as the whole year.
FULL_YEAR_DAY: Final = 7
#: What a major unit holds of the minor one (öre, cent): every area SpotNav prices has a hundred.
MINOR_PER_MAJOR: Final = 100
MIN_SAVED_DAYS: Final = 7
MIN_PRICED_CAR_KWH: Final = 50.0
PRICED_CAR_SHARE: Final = 0.8
MIN_CHEAPEST_KWH: Final = 5.0
MIN_BATTERY_DAYS: Final = 14
MIN_FUSE_DAYS: Final = 28
MIN_FUSE_COUNT: Final = 3
MIN_PRICE_DAYS: Final = 28
FULL_PRICE_DAYS: Final = 300
#: A clock hour counts for the most expensive one when its slots cover this much of it (seconds).
_HOUR_COVERED_S: Final = 3540.0
#: The decimals a clock hour's mean sell and buy prices are kept to in the year store (minor per kWh).
SELL_DIGITS: Final = 3


# ------------------------------------------------------------------------------------------------ the year


def in_run_window(today: date) -> bool:
    """Whether the nightly run works the review out today: 10 to 31 December, and 1 January (the final one)."""
    return (today.month == 12 and today.day >= WINDOW_DAY) or (today.month == 1 and today.day == 1)


def in_show_window(today: date) -> bool:
    """Whether the cards offer the review today: 10 December to 31 January."""
    return (today.month == 12 and today.day >= WINDOW_DAY) or today.month == 1


def review_year(today: date) -> int:
    """The year a review made today is of: January's is the year before, every other month's the year so far."""
    return today.year - 1 if today.month == 1 else today.year


def year_bounds(year: int, today: date) -> tuple[date, date] | None:
    """The year's first day and the last day the review covers (31 December of a past year, yesterday of this one);
    `None` for a year with no day behind it yet."""
    first = date(year, 1, 1)
    through = date(year, 12, 31) if year < today.year else today - timedelta(days=1)
    return (first, through) if year <= today.year and through >= first else None


# ------------------------------------------------------------------------------------------------ the inputs


def _midnight(day: date, zone: tzinfo) -> datetime:
    """A local day's first instant (UTC)."""
    return datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class PriceDay:
    """One local day's prices as the review reads them (`price_day_of`), worked out once the day is over with that
    day's fees and kept in the year store, so the year is never read slot by slot:

    * `runs`: the day's price slots in order, as `(offset from the day's first instant, slot length, count)` in
      seconds (a whole day of quarters is one run); `cheap`: a bit per slot, in that order, set for the slots in the
      day's cheapest third (`cheap_slots`). The car page's share.
    * `sells`: each local clock hour's mean sell price from the day's first hour (minor per kWh, `SELL_DIGITS`
      decimals: only whether the year's export earned anything is shown), `None` for an hour without a slot. The sun
      page's `export_paid`.
    * `buys`: each local clock hour's mean buy price the same way. The battery page's `saved`. `None` for a part
      kept before the buys were (stored without `b`): it is worked out again while its day's prices are held
      (`runtime`), and set to `()` where they no longer are.
    * `top`: the day's most expensive clock hour that the slots cover (`_HOUR_COVERED_S`), `(hour from the day's
      first, mean buy price)`, `None` without one. The expensive hour, and the days counted priced.

    No slot: an empty part (the day was looked at and had no price)."""

    runs: tuple[tuple[int, int, int], ...] = ()
    cheap: int = 0
    sells: tuple[float | None, ...] = ()
    top: tuple[int, float] | None = None
    buys: tuple[float | None, ...] | None = ()

    @property
    def covered_s(self) -> int:
        """The seconds the day's slots cover."""
        return sum(seconds * count for _offset, seconds, count in self.runs)

    def intervals(self, day: date, zone: tzinfo) -> list[tuple[datetime, datetime, bool]]:
        """The day's slots again: `(start, end, in the cheapest third)`, in order."""
        midnight = _midnight(day, zone)
        out: list[tuple[datetime, datetime, bool]] = []
        for offset, seconds, count in self.runs:
            for step in range(count):
                start = midnight + timedelta(seconds=offset + step * seconds)
                out.append((start, start + timedelta(seconds=seconds), bool(self.cheap >> len(out) & 1)))
        return out

    def hour_sells(self, day: date, zone: tzinfo) -> dict[datetime, float]:
        """Each clock hour's mean sell price, by its UTC start."""
        midnight = _midnight(day, zone)
        return {midnight + timedelta(hours=index): sell for index, sell in enumerate(self.sells) if sell is not None}

    def hour_buys(self, day: date, zone: tzinfo) -> dict[datetime, float]:
        """Each clock hour's mean buy price, by its UTC start (none for a part kept without them)."""
        midnight = _midnight(day, zone)
        return {midnight + timedelta(hours=index): buy for index, buy in enumerate(self.buys or ()) if buy is not None}

    def top_hour(self, day: date, zone: tzinfo) -> tuple[datetime, float] | None:
        """The day's most expensive covered clock hour (its UTC start) and its mean buy price."""
        if self.top is None:
            return None
        return _midnight(day, zone) + timedelta(hours=self.top[0]), self.top[1]

    def as_dict(self) -> dict[str, Any]:
        """`{r: [[offset, seconds, count], ...], c: cheap bits in hex, s: [sell | null, ...], b: [buy | null, ...],
        t: [hour, buy]}`, `{}` for a day without a slot (`b` left out of a part kept before the buys were)."""
        if not self.runs:
            return {}
        out: dict[str, Any] = {
            "r": [list(run) for run in self.runs],
            "c": format(self.cheap, "x"),
            "s": list(self.sells),
        }
        if self.buys is not None:
            out["b"] = list(self.buys)
        if self.top is not None:
            out["t"] = [self.top[0], self.top[1]]
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> PriceDay | None:
        """The part again; `None` for one that cannot be read (it is worked out again where the prices are held)."""
        if not isinstance(raw, Mapping):
            return None
        if not raw:
            return cls()
        try:
            runs = tuple((int(run[0]), int(run[1]), int(run[2])) for run in raw["r"] if len(run) == 3)
            cheap = int(raw.get("c") or "0", 16)
            sells = tuple(None if sell is None else float(sell) for sell in raw.get("s") or ())
            buys_raw = raw.get("b")
            buys = None if buys_raw is None else tuple(None if buy is None else float(buy) for buy in buys_raw)
            top_raw = raw.get("t")
            top = None if top_raw is None else (int(top_raw[0]), float(top_raw[1]))
        except KeyError, TypeError, ValueError, IndexError:
            return None
        if not runs or any(count <= 0 for _offset, _seconds, count in runs) or cheap < 0:
            return None
        if any(price is not None and not math.isfinite(price) for price in (*sells, *(buys or ()))):
            return None
        if top is not None and not math.isfinite(top[1]):
            return None
        return cls(runs=runs, cheap=cheap, sells=sells, top=top, buys=buys)


def price_day_of(day: date, slots: Sequence[PriceSlot], zone: tzinfo) -> PriceDay:
    """Day `day`'s part from its price slots (those that start on it, local; `pricing/period_prices`)."""
    ordered = sorted(slots, key=lambda slot: slot.start)
    if not ordered:
        return PriceDay()
    midnight = _midnight(day, zone)
    runs: list[list[int]] = []
    for slot in ordered:
        offset = round((slot.start - midnight).total_seconds())
        seconds = round((slot.end - slot.start).total_seconds())
        last = runs[-1] if runs else None
        if last is not None and last[1] == seconds and last[0] + seconds * last[2] == offset:
            last[2] += 1
        else:
            runs.append([offset, seconds, 1])
    cheap = cheap_slots(ordered, zone)
    bits = sum(1 << index for index, slot in enumerate(ordered) if slot.start in cheap)
    hours = {round((hour - midnight).total_seconds() / 3600): row for hour, row in _hour_prices(ordered, zone).items()}
    count = max(hours) + 1 if hours else 0
    sells = tuple(None if index not in hours else round(hours[index][2], SELL_DIGITS) for index in range(count))
    buys = tuple(None if index not in hours else round(hours[index][1], SELL_DIGITS) for index in range(count))
    held = {index: row for index, row in hours.items() if row[0] >= _HOUR_COVERED_S}
    top = None
    if held:
        index = max(held, key=lambda index: (held[index][1], -index))
        top = (index, held[index][1])
    return PriceDay(runs=tuple((run[0], run[1], run[2]) for run in runs), cheap=bits, sells=sells, top=top, buys=buys)


def price_days_of(slots: Iterable[PriceSlot], zone: tzinfo) -> dict[date, PriceDay]:
    """Each local day's part from a stretch's price slots (`price_day_of`)."""
    by_day: dict[date, list[PriceSlot]] = {}
    for slot in slots:
        by_day.setdefault(slot.start.astimezone(zone).date(), []).append(slot)
    return {day: price_day_of(day, day_slots, zone) for day, day_slots in sorted(by_day.items())}


@dataclass(frozen=True, slots=True)
class DayParts:
    """One local day's parts as the year store keeps them (minor units; `None`: not a part of the day): what SpotNav
    saved on the battery, the car (measured since 3.0) and the heat, the car's day-average estimate for the charges
    before 3.0 (`car_est`), and the fuse's overload episodes counted that day."""

    battery: float | None = None
    car: float | None = None
    car_est: float | None = None
    heat: float | None = None
    fuse: int = 0
    #: The day's prices as the review reads them (`PriceDay`), worked out once the day is over; `None` not yet.
    price: PriceDay | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in ("battery", "car", "car_est", "heat"):
            value = getattr(self, name)
            if value is not None:
                out[name] = round(value, 2)
        if self.fuse:
            out["fuse"] = self.fuse
        if self.price is not None:
            out["price"] = self.price.as_dict()
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> DayParts | None:
        if not isinstance(raw, Mapping):
            return None
        values: dict[str, Any] = {}
        for name in ("battery", "car", "car_est", "heat"):
            value = raw.get(name)
            if _number(value):
                values[name] = float(value)
        fuse = raw.get("fuse")
        if isinstance(fuse, int) and not isinstance(fuse, bool) and fuse > 0:
            values["fuse"] = fuse
        if (price := PriceDay.from_dict(raw.get("price"))) is not None:
            values["price"] = price
        return cls(**values)

    def merged(self, newer: DayParts) -> DayParts:
        """These parts with `newer`'s laid over them: a part `newer` has replaces this one's, one it lacks is kept
        (a source that no longer holds the day never takes it away). The fuse's count is the store's own: kept."""
        return replace(
            self,
            battery=self.battery if newer.battery is None else newer.battery,
            car=self.car if newer.car is None else newer.car,
            car_est=self.car_est if newer.car_est is None else newer.car_est,
            heat=self.heat if newer.heat is None else newer.heat,
            price=self.price if newer.price is None else newer.price,
        )

    @property
    def any(self) -> bool:
        """Whether the day has a saving part (the price part and the fuse's count are not savings)."""
        return any(value is not None for value in (self.battery, self.car, self.car_est, self.heat))


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def estimate_days(sessions: Iterable[ChargeSession], zone: tzinfo) -> dict[date, float]:
    """The car's day-average estimate per local day for the charges SpotNav started before it knew the plug-in (a
    live record without `plugged_in_at`, started by the plan, the sun or the hybrid): what the priced energy would
    have cost at the day's average price less what it cost (`ChargeSession.savings_minor`), on the day it started."""
    out: dict[date, float] = {}
    for session in sessions:
        if session.imported or session.plugged_in_at is not None or session.started_by not in PLANNED:
            continue
        saving = session.savings_minor
        if saving is None or not math.isfinite(saving):
            continue
        day = session.start.astimezone(zone).date()
        out[day] = out.get(day, 0.0) + saving
    return out


def day_parts_of(
    savings: Mapping[date, DaySaving], estimates: Mapping[date, float]
) -> dict[date, DayParts]:
    """The day parts to merge into the year store from what SpotNav saved per day (`api/saving.async_day_savings`)
    and the car's estimates (`estimate_days`)."""
    out: dict[date, DayParts] = {}
    for day in sorted(set(savings) | set(estimates)):
        saving = savings.get(day)
        parts = DayParts(
            battery=None if saving is None else saving.battery,
            car=None if saving is None else saving.car,
            car_est=estimates.get(day),
            heat=None if saving is None else saving.heat,
        )
        if parts.any:
            out[day] = parts
    return out


@dataclass(frozen=True, slots=True)
class YearInputs:
    year: int
    today: date
    tz: str
    #: The area's money words `(currency, major, minor)`; `None` without a price source.
    money: tuple[str, str | None, str | None] | None = None
    #: The first day of SpotNav's own record (the site's set-up, the first charge recorded live); `None` unknown.
    record_start: date | None = None
    #: The year's energy by the hour (`api/house_history.history_with_periods`), `None` without the grid meter.
    hours: Sequence[EnergyHour] | None = None
    has_solar: bool = False
    has_export: bool = False
    has_battery: bool = False
    #: The site's chargers' sessions, costed with the current settings.
    sessions: Sequence[ChargeSession] = ()
    #: The year store's day parts (merged with tonight's), each day's prices among them (`DayParts.price`).
    day_parts: Mapping[date, DayParts] = field(default_factory=dict)
    #: The battery's money per day (`battery/stored_value.DayMoney`: `arbitrage`, `flow`).
    battery_days: Mapping[date, Any] = field(default_factory=dict)
    capacity_kwh: float | None = None
    #: The day the fuse's episode counter started, `None` before it.
    fuse_since: date | None = None


# ------------------------------------------------------------------------------------------------ the review


def _r(value: float | None, digits: int = 0) -> float | None:
    if value is None:
        return None
    rounded = round(value, digits)
    return int(rounded) if digits == 0 else rounded


def _later_than_full(day: date | None, year: int) -> date | None:
    """`day` where it is after the year's `FULL_YEAR_DAY` January, else `None` (a record that starts on time)."""
    return day if day is not None and day > date(year, 1, FULL_YEAR_DAY) else None


def _iso(day: date | None) -> str | None:
    return None if day is None else day.isoformat()


def compute_review(inputs: YearInputs) -> dict[str, Any] | None:
    """The review of `inputs.year` (the module's shape), `None` for a year with no day behind it yet."""
    bounds = year_bounds(inputs.year, inputs.today)
    if bounds is None:
        return None
    first, through = bounds
    zone = time_zones.require_zone(inputs.tz)
    since = _later_than_full(inputs.record_start, inputs.year)
    hours = [hour for hour in (inputs.hours or ()) if first <= hour.start.astimezone(zone).date() <= through]
    prices = {
        day: parts.price
        for day, parts in sorted(inputs.day_parts.items())
        if first <= day <= through and parts.price is not None
    }
    pages: list[dict[str, Any] | None] = [
        {"id": "cover"},
        saved_page(inputs, first, through),
        car_page(inputs, first, through, prices, zone),
        sun_page(inputs, hours, prices, zone),
        battery_page(inputs, hours, first, through, prices, zone),
        fuse_page(inputs, first, through),
        expensive_page(inputs, hours, prices, zone),
        {"id": "thanks"},
    ]
    money = inputs.money
    return {
        "year": inputs.year,
        "through": through.isoformat(),
        "since": _iso(since),
        "partial": since is not None,
        "money": None
        if money is None
        else {"currency": money[0], "major_unit": money[1], "minor_unit": money[2]},
        "pages": [page for page in pages if page is not None],
    }


def saved_page(inputs: YearInputs, first: date, through: date) -> dict[str, Any] | None:
    sums: dict[str, float | None] = {"battery": None, "car": None, "heat": None}
    starts: dict[str, date | None] = {"battery": None, "car": None, "heat": None}
    days = 0
    estimated = False
    for day, parts in sorted(inputs.day_parts.items()):
        if not first <= day <= through or not parts.any:
            continue
        days += 1
        car = None
        if parts.car is not None or parts.car_est is not None:
            car = (parts.car or 0.0) + (parts.car_est or 0.0)
        estimated = estimated or bool(parts.car_est)
        for name, value in (("battery", parts.battery), ("car", car), ("heat", parts.heat)):
            if value is None:
                continue
            sums[name] = (sums[name] or 0.0) + value
            if starts[name] is None:
                starts[name] = day
    known = [value for value in sums.values() if value is not None]
    total = sum(known) if known else None
    if total is None or total < MINOR_PER_MAJOR or days < MIN_SAVED_DAYS:
        return None
    return {
        "id": "saved",
        "total": _r(total),
        "battery": _r(sums["battery"]),
        "car": _r(sums["car"]),
        "heat": _r(sums["heat"]),
        "estimated": estimated,
        "starts": {name: _iso(_later_than_full(day, inputs.year)) for name, day in starts.items()},
    }


def cheap_slots(slots: Sequence[PriceSlot], zone: tzinfo) -> set[datetime]:
    """The starts of the slots in the cheapest third of their local day's slots (by the buy price, the earlier first
    on a tie), at least one a day."""
    by_day: dict[date, list[PriceSlot]] = {}
    for slot in slots:
        by_day.setdefault(slot.start.astimezone(zone).date(), []).append(slot)
    out: set[datetime] = set()
    for day_slots in by_day.values():
        ordered = sorted(day_slots, key=lambda slot: (slot.buy, slot.start))
        count = max(1, round(len(ordered) / 3))
        out.update(slot.start for slot in ordered[:count])
    return out


class _Cover:
    """How much of a stretch the slots `(start, end, cheap)` cover, in order, and how much of it in the cheap ones."""

    def __init__(self, slots: Sequence[tuple[datetime, datetime, bool]]) -> None:
        self.slots = slots
        self.starts = [slot[0] for slot in slots]

    def of(self, start: datetime, end: datetime) -> tuple[float, float]:
        covered = cheap = 0.0
        low_index = max(0, bisect_left(self.starts, start - timedelta(hours=2)))
        for slot_start, slot_end, in_cheap in self.slots[low_index : bisect_right(self.starts, end)]:
            low, high = max(start, slot_start), min(end, slot_end)
            if high <= low:
                continue
            seconds = (high - low).total_seconds()
            covered += seconds
            if in_cheap:
                cheap += seconds
        return covered, cheap


def car_page(
    inputs: YearInputs, first: date, through: date, prices: Mapping[date, PriceDay], zone: tzinfo
) -> dict[str, Any] | None:
    sessions = sorted(
        (session for session in inputs.sessions if first <= session.start.astimezone(zone).date() <= through),
        key=lambda session: session.start,
    )
    if not sessions:
        return None
    cover = _Cover([slot for day in sorted(prices) for slot in prices[day].intervals(day, zone)])
    total_kwh = sum(max(0.0, session.energy_kwh) for session in sessions)
    priced_kwh = cheap_kwh = 0.0
    first_priced: date | None = None
    for session in sessions:
        for piece in session.intervals:
            span = (piece.end - piece.start).total_seconds()
            if span <= 0 or piece.kwh <= 0:
                continue
            covered, cheap = cover.of(piece.start, piece.end)
            if covered < span - 1:
                continue
            priced_kwh += piece.kwh
            cheap_kwh += piece.kwh * min(1.0, cheap / span)
            day = piece.start.astimezone(zone).date()
            if first_priced is None or day < first_priced:
                first_priced = day
    cheapest: tuple[float, date] | None = None
    for session in sessions:
        price = session.average_price_minor_per_kwh
        if (
            price is None
            or session.energy_kwh < MIN_CHEAPEST_KWH
            or session.priced_kwh < session.energy_kwh - 0.01
            or not math.isfinite(price)
        ):
            continue
        if cheapest is None or price < cheapest[0]:
            cheapest = (price, session.start.astimezone(zone).date())
    share = None if priced_kwh < MIN_PRICED_CAR_KWH else 100.0 * cheap_kwh / priced_kwh
    return {
        "id": "car",
        "cheap_share_pct": _r(share),
        "charges": len(sessions),
        "kwh": _r(total_kwh),
        "cheapest": None if cheapest is None else {"price": _r(cheapest[0], 2), "date": cheapest[1].isoformat()},
        "estimated": any(session.imported for session in sessions),
        "since": _iso(first_priced) if share is not None and priced_kwh < PRICED_CAR_SHARE * total_kwh else None,
    }


def _hour_prices(slots: Sequence[PriceSlot], zone: tzinfo) -> dict[datetime, tuple[float, float, float]]:
    """Each local clock hour the slots cover (its UTC start): `(covered seconds, mean buy, mean sell)`."""
    sums: dict[datetime, list[float]] = {}
    for slot in slots:
        local = slot.start.astimezone(zone)
        hour = local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        seconds = (slot.end - slot.start).total_seconds()
        if seconds <= 0:
            continue
        row = sums.setdefault(hour, [0.0, 0.0, 0.0])
        row[0] += seconds
        row[1] += seconds * slot.buy
        row[2] += seconds * slot.sell
    return {hour: (row[0], row[1] / row[0], row[2] / row[0]) for hour, row in sums.items() if row[0] > 0}


def sun_page(
    inputs: YearInputs, hours: Sequence[EnergyHour], prices: Mapping[date, PriceDay], zone: tzinfo
) -> dict[str, Any] | None:
    if not inputs.has_solar:
        return None
    read = [hour for hour in hours if hour.solar is not None]
    if not read:
        return None
    produced = sum(max(0.0, hour.solar or 0.0) for hour in read)
    out_kwh = 0.0
    earned = 0.0
    priced = False
    sells: dict[datetime, float] = {}
    for day, price in prices.items():
        sells.update(price.hour_sells(day, zone))
    for hour in read:
        exported = allocate_hour(hour).get((ROLE_SOLAR, ROLE_GRID), 0.0)
        out_kwh += exported
        sell = sells.get(hour.start)
        if sell is not None:
            priced = True
            earned += exported * sell
    home = None
    if inputs.has_export and produced > 0:
        home = 100.0 * max(0.0, produced - out_kwh) / produced
    export_paid: bool | None = None
    if inputs.has_export and priced:
        export_paid = out_kwh > 0 and earned > 0
    first_day = min(hour.start for hour in read).astimezone(zone).date()
    return {
        "id": "sun",
        "kwh": _r(produced),
        "home_pct": _r(home),
        "export_paid": export_paid,
        "since": _iso(_later_than_full(first_day, inputs.year)),
    }


def battery_saved(
    hours: Sequence[EnergyHour], prices: Mapping[date, PriceDay], zone: tzinfo
) -> tuple[float, set[date]]:
    """What the battery saved over the hours whatever controlled it (minor units), and the local days of the priced
    hours with a battery reading: each whole priced hour's discharge at its mean buy price, less its charge, the part
    the grid's import covers (`min(battery_in, grid_in)`) at the buy price and the rest at the sell price (the export
    the sun's energy gave up)."""
    buys: dict[datetime, float] = {}
    sells: dict[datetime, float] = {}
    for day, price in prices.items():
        buys.update(price.hour_buys(day, zone))
        sells.update(price.hour_sells(day, zone))
    total = 0.0
    days: set[date] = set()
    for hour in hours:
        if hour.minutes != 60 or (hour.battery_in is None and hour.battery_out is None):
            continue
        buy = buys.get(hour.start)
        sell = sells.get(hour.start)
        if buy is None or sell is None:
            continue
        charged = max(0.0, hour.battery_in or 0.0)
        given = max(0.0, hour.battery_out or 0.0)
        from_grid = min(charged, max(0.0, hour.grid_in))
        total += given * buy - from_grid * buy - (charged - from_grid) * sell
        days.add(hour.start.astimezone(zone).date())
    return total, days


def battery_page(
    inputs: YearInputs,
    hours: Sequence[EnergyHour],
    first: date,
    through: date,
    prices: Mapping[date, PriceDay] | None = None,
    zone: tzinfo | None = None,
) -> dict[str, Any] | None:
    kwh_in = kwh_out = None
    if inputs.has_battery:
        ins = [hour.battery_in for hour in hours if hour.battery_in is not None]
        outs = [hour.battery_out for hour in hours if hour.battery_out is not None]
        kwh_in = sum(max(0.0, value) for value in ins) if ins else None
        kwh_out = sum(max(0.0, value) for value in outs) if outs else None
    counted = sorted((day, money) for day, money in inputs.battery_days.items() if first <= day <= through)
    if kwh_in is None and kwh_out is None and not counted:
        return None
    earned = buy = use = None
    earned_since = None
    if len(counted) >= MIN_BATTERY_DAYS:
        earned = sum(float(getattr(money, "arbitrage", 0.0) or 0.0) for _day, money in counted)
        earned_since = _later_than_full(counted[0][0], inputs.year)
        charged = cost = discharged = worth = 0.0
        for _day, money in counted:
            flow = getattr(money, "flow", None)
            if flow is None:
                continue
            charged += flow.charged
            cost += flow.charge_cost
            discharged += flow.discharged
            worth += flow.worth
        buy = cost / charged if charged > 0 else None
        use = worth / discharged if discharged > 0 else None
    saved = saved_since = None
    saved_start: date | None = None
    if inputs.has_battery and prices and zone is not None:
        value, priced_days = battery_saved(hours, prices, zone)
        if len(priced_days) >= MIN_BATTERY_DAYS and value >= MINOR_PER_MAJOR:
            saved = value
            saved_start = min(priced_days)
            saved_since = _later_than_full(saved_start, inputs.year)
    # The cycles cover the same days as the money the page shows (the count's first day, else the priced hours'),
    # so the page never mixes a short span's kr with a longer span's cycles; without money, the whole year read.
    money_start = counted[0][0] if earned is not None else saved_start
    cycles = None
    if kwh_out is not None and inputs.capacity_kwh and inputs.capacity_kwh > 0:
        given = kwh_out
        if money_start is not None:
            given = sum(
                max(0.0, hour.battery_out)
                for hour in hours
                if hour.battery_out is not None
                and (hour.start.astimezone(zone).date() if zone is not None else hour.start.date()) >= money_start
            )
        cycles = given / inputs.capacity_kwh
    return {
        "id": "battery",
        "earned": _r(earned),
        "earned_since": _iso(earned_since),
        "saved": _r(saved),
        "saved_since": _iso(saved_since),
        "cycles": _r(cycles),
        "buy_price": _r(buy, 1),
        "use_price": _r(use, 1),
        "kwh_in": _r(kwh_in),
        "kwh_out": _r(kwh_out),
    }


def fuse_page(inputs: YearInputs, first: date, through: date) -> dict[str, Any] | None:
    if inputs.fuse_since is None or inputs.fuse_since > through:
        return None
    since = max(first, inputs.fuse_since)
    if (through - since).days + 1 < MIN_FUSE_DAYS:
        return None
    count = sum(parts.fuse for day, parts in inputs.day_parts.items() if since <= day <= through)
    if count < MIN_FUSE_COUNT:
        return None
    return {"id": "fuse", "count": count, "since": _iso(_later_than_full(since, inputs.year))}


def expensive_page(
    inputs: YearInputs, hours: Sequence[EnergyHour], prices: Mapping[date, PriceDay], zone: tzinfo
) -> dict[str, Any] | None:
    tops = {day: found for day, price in prices.items() if (found := price.top_hour(day, zone)) is not None}
    days = set(tops)
    if len(days) < MIN_PRICE_DAYS:
        return None
    top, top_buy = max(tops.values(), key=lambda found: (found[1], -found[0].timestamp()))
    bought = next((row.grid_in for row in hours if row.start == top and row.minutes == 60), None)
    return {
        "id": "expensive",
        "price": _r(top_buy, 1),
        "start": top.astimezone(zone).isoformat(),
        "bought_kwh": None if bought is None else _r(max(0.0, bought), 2),
        "since": _iso(min(days)) if len(days) < FULL_PRICE_DAYS else None,
    }


# ------------------------------------------------------------------------------------------------ the answer


def review_answer(
    *, site_id: str, year: int, state: str, banner: bool, row: bool, review: dict[str, Any] | None
) -> dict[str, Any]:
    """`spotnav/get_year_review`'s answer (the module's shape): the review only when `state` is ready."""
    ready = state == STATE_READY and review is not None
    return {
        "api_version": YEAR_REVIEW_API_VERSION,
        "site_id": site_id,
        "year": year,
        "state": state,
        "banner": bool(banner and ready),
        "row": bool(row and ready),
        "review": review if ready else None,
    }
