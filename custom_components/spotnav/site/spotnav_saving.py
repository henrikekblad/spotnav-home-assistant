"""What SpotNav saved (SpotNav 3.0): the battery, the car and the heat, each against a counterfactual of its own, per
local day. The house card's line ("SpotNav sparade 54 kr i dag · batteri 35 · bil 17 · värme 2") and its history
sum these days; the charger card's history shows the car's figure per session from the same computation
(`session_savings`). Pure: the shells (`api/saving.py`, `api/house.py`, `api/house_history.py`) read the parts.

**The battery** (`battery_part`): the battery's arbitrage of the day (`battery/stored_value.py`: what each kWh it
gave back earned over its stored cost, the house card's headline "Batteriet har tjänat", the measure Sigenergy's app
shows), in the share of the day's counted quarter hours SpotNav controlled the battery (`DayMoney.controlled`: the
battery control `on` when the quarter hour was counted). A day the battery ran under its own control (SpotNav off,
in test or still checking it; Sigenergy's AI mode) has no battery part: what it earned is the battery's own and the
card's headline says so ("egen styrning"), never inside SpotNav's total.

**The car** (`session_savings`): against charging at once on plug-in. A plug-in is the sessions of one charger with
the same `plugged_in_at`. Its counterfactual delivers the same kWh from the plug-in on at the plug-in's measured power
(the highest rate any of its price intervals drew, else its energy over its charging time), until that energy is in or
the last session ended; the counterfactual price is the mean buy price (every fee in it, `PriceSlot.buy`, the house
card's pricing) over that schedule. A session SpotNav started (`plan_window`, `solar`, `hybrid`) saves, per piece of
its energy, `grid kWh × (counterfactual price − the piece's buy price)`; a session a person started, or one whose
start nobody recorded, saves 0 (its energy still counts in the counterfactual's and the car's cost). A session with
no plug-in time (a record from before it, a charger that cannot say whether a car is connected) or without the
counterfactual's prices has no figure (`None`), never 0. A session that started at plug-in anyway saves about 0.

* **Day**: a session's saving is counted on the local day its charge started, as the charger card's history counts a
  session's cost, so a charge over midnight is one figure on one day in both cards. A day's hourly bars put it in the
  hour the charge started (`car_hours`).
* **The battery's and the sun's kWh** (no double counting): only the car's grid kWh count. The energy flow's rule
  (`site/energy_flow.allocate_hour`, hour by hour from the recorder's long-term statistics) says how much of the car's
  energy in an hour came from the grid; the kWh the battery gave the car are already in the battery's arbitrage (it
  values every kWh it gave the house, the car in it, at the buy price), and the sun's kWh are worth the sell price
  whenever they are used. So both are left out of the car's saving, in the actual and the counterfactual alike, and
  the battery's figure holds no part of the car's. An hour with no flow figure counts all its kWh as the grid's on a
  site with neither battery nor sun, and none on one with either (`default_share`).

**The heat** (`heat_part`): the heat control's own review of the day (`heat/review.py`, the relay's closings: what
each lowered hour took away at its price, less the learned rebound bought back after), in the share of the relay's
closed time SpotNav itself held it closed (`spotnav_min`, kept with the review, `heat/runtime.py`). What SpotNav's plan
would have done in shadow (`review.plan`) never counts: nothing was done. An estimate, and labelled one. A day is
known the night after (the review needs the rebound's hours), so today has no heat part yet.

**Before SpotNav controls the battery** (`own_control`): on a day (or the share of one) the battery ran under its
own control, what it earned is the battery's own (`own`: its arbitrage in that share), and beside it stands what
SpotNav's plan would have earned on the same measured day (`would`): the same figure plus what the nightly review
(`battery/plan_shadow.DayReview`) says the plan's day would have cost less than the day as it went (the plan's saving
less the battery's own, both against plain self-consumption), in the same share. The review comes the night after, so
a day without one has no `would`. Never inside SpotNav's total: nothing was done.

A part a site does not have, or not yet for the day, is `None` and left out of the total, never shown as 0.
Money is in the area's minor unit (öre).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Final

from ..sessions.model import STARTED_HYBRID, STARTED_PLAN_WINDOW, STARTED_SOLAR, ChargeSession
from .energy_flow import ROLE_CAR, ROLE_GRID, EnergyHour, allocate_hour
from .session_car import energy_spans

#: The sessions SpotNav started: the only ones that can have saved anything.
PLANNED: Final = frozenset({STARTED_PLAN_WINDOW, STARTED_SOLAR, STARTED_HYBRID})
#: A price interval drawn for less than this is too short to say the car's power.
MIN_RATE_SPAN: Final = timedelta(minutes=5)

SAVING_KEYS: Final = ("date", "total", "battery", "car", "heat", "battery_share", "car_since")
PARTS: Final = ("battery", "car", "heat")


@dataclass(frozen=True, slots=True)
class DaySaving:
    """One local day's parts (minor units, `None`: not a part of the day) and the share of the day SpotNav controlled
    the battery (`None` without the battery's day)."""

    battery: float | None = None
    car: float | None = None
    heat: float | None = None
    battery_share: float | None = None

    @property
    def total(self) -> float | None:
        parts = [value for value in (self.battery, self.car, self.heat) if value is not None]
        return sum(parts) if parts else None


# ------------------------------------------------------------------------------------------------ prices and shares


class Prices:
    """The buy price over any stretch from sorted price slots."""

    def __init__(self, slots: Iterable[Any]) -> None:
        self.slots = sorted(slots, key=lambda slot: slot.start)
        self.starts = [slot.start for slot in self.slots]

    def pieces(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime, float]]:
        """The slots' parts over `[start, end)`: `(from, to, buy)`; a stretch without a slot is left out."""
        out = []
        for slot in self.slots[max(0, bisect_left(self.starts, start - timedelta(days=1))) : bisect_right(self.starts, end)]:
            low, high = max(start, slot.start), min(end, slot.end)
            if high > low:
                out.append((low, high, float(slot.buy)))
        return out

    def mean(self, start: datetime, end: datetime) -> float | None:
        """The time-weighted buy price over `[start, end)`; `None` when a part of it has no slot."""
        pieces = self.pieces(start, end)
        covered = sum((high - low).total_seconds() for low, high, _buy in pieces)
        span = (end - start).total_seconds()
        if span <= 0 or covered < span - 1:
            return None
        return sum((high - low).total_seconds() * buy for low, high, buy in pieces) / covered


def car_grid_share(period: EnergyHour) -> float | None:
    """The share of the cars' energy in the period that came from the grid by the energy flow's rule; where the flow
    gives the cars nothing (their figure from elsewhere), the grid's share of everything supplied; `None` with
    nothing supplied."""
    flows = allocate_hour(period)
    car = sum(kwh for (_source, use), kwh in flows.items() if use == ROLE_CAR)
    if car > 0:
        return flows.get((ROLE_GRID, ROLE_CAR), 0.0) / car
    supply = max(0.0, period.grid_in) + max(0.0, period.battery_out or 0.0) + max(0.0, period.solar or 0.0)
    return max(0.0, period.grid_in) / supply if supply > 0 else None


class GridShares:
    """The cars' grid share over any stretch, from the energy flow's periods (see the module)."""

    def __init__(self, periods: Iterable[EnergyHour], default_share: float) -> None:
        rows = []
        for period in periods:
            share = car_grid_share(period)
            if share is not None:
                rows.append((period.start, period.start + timedelta(minutes=period.minutes), share))
        self.rows = sorted(rows)
        self.starts = [row[0] for row in self.rows]
        self.default = default_share

    def share(self, start: datetime, end: datetime) -> float:
        span = (end - start).total_seconds()
        if span <= 0:
            return self.default
        known = weighted = 0.0
        for low, high, share in self.rows[max(0, bisect_left(self.starts, start - timedelta(hours=1))) : bisect_right(self.starts, end)]:
            overlap = (min(end, high) - max(start, low)).total_seconds()
            if overlap > 0:
                known += overlap
                weighted += overlap * share
        return (weighted + max(0.0, span - known) * self.default) / span


def default_share(has_battery: bool, has_sun: bool) -> float:
    """An hour without a flow figure: all grid on a site with neither battery nor sun, else none (see the module)."""
    return 0.0 if has_battery or has_sun else 1.0


# ------------------------------------------------------------------------------------------------ the car


def plug_ins(sessions: Iterable[ChargeSession]) -> list[list[ChargeSession]]:
    """The sessions grouped by plug-in (charger and `plugged_in_at`), each in order; a session with no plug-in time
    alone."""
    groups: dict[Any, list[ChargeSession]] = {}
    for session in sessions:
        key = (session.charger_id, session.plugged_in_at) if session.plugged_in_at is not None else ("", session.id)
        groups.setdefault(key, []).append(session)
    return [sorted(group, key=lambda item: item.start) for group in groups.values()]


def _end(session: ChargeSession, now: datetime) -> datetime:
    return session.end or session.last_sample_at or now


def plug_in_kw(group: Sequence[ChargeSession], now: datetime) -> float | None:
    """The plug-in's measured power (kW): the highest rate a price interval drew, else its energy over its charging."""
    best = 0.0
    for session in group:
        for start, end, kwh in energy_spans(session):
            if end - start >= MIN_RATE_SPAN:
                best = max(best, kwh / ((end - start).total_seconds() / 3600.0))
    if best > 0:
        return best
    energy = sum(session.energy_kwh for session in group)
    seconds = sum(max(0.0, (_end(session, now) - session.start).total_seconds()) for session in group)
    return energy / (seconds / 3600.0) if energy > 0 and seconds > 0 else None


def at_once_price(group: Sequence[ChargeSession], prices: Prices, now: datetime) -> float | None:
    """The mean buy price had the plug-in's energy been charged at once (see the module); `None` without a plug-in
    time, a power or the prices."""
    plugged = group[0].plugged_in_at
    energy = sum(session.energy_kwh for session in group)
    kw = plug_in_kw(group, now)
    if plugged is None or kw is None or kw <= 0 or energy <= 0:
        return None
    end = plugged + timedelta(hours=energy / kw)
    last = max(_end(session, now) for session in group)
    end = min(end, max(last, plugged + timedelta(minutes=1)))
    return prices.mean(plugged, end)


def session_savings(
    sessions: Iterable[ChargeSession], prices: Prices, shares: GridShares, now: datetime
) -> dict[str, float | None]:
    """Each session's saving on the car (minor units) against charging at once on plug-in (see the module): 0 for a
    session SpotNav did not start, `None` where it cannot be known."""
    out: dict[str, float | None] = {}
    for group in plug_ins(sessions):
        dumb = at_once_price(group, prices, now)
        for session in group:
            if session.plugged_in_at is None:
                out[session.id] = None
            elif session.started_by not in PLANNED:
                out[session.id] = 0.0
            elif dumb is None:
                out[session.id] = None
            else:
                saving = 0.0
                for start, end, kwh in energy_spans(session):
                    length = (end - start).total_seconds()
                    for low, high, buy in prices.pieces(start, end):
                        piece = kwh * (high - low).total_seconds() / length
                        saving += piece * shares.share(low, high) * (dumb - buy)
                out[session.id] = saving
    return out


def car_days(
    sessions: Iterable[ChargeSession], savings: Mapping[str, float | None], zone: tzinfo
) -> tuple[dict[date, float], date | None]:
    """The car's saving per local day its sessions started, and the first day it is known from (the first plug-in
    recorded with its time; `None` before any): a day from then on without a figure is 0."""
    sessions = list(sessions)
    recorded = [session.plugged_in_at for session in sessions if session.plugged_in_at is not None]
    since = min(recorded).astimezone(zone).date() if recorded else None
    out: dict[date, float] = {}
    for session in sessions:
        value = savings.get(session.id)
        if value is not None:
            day = session.start.astimezone(zone).date()
            out[day] = out.get(day, 0.0) + value
    return out, since


def car_hours(sessions: Iterable[ChargeSession], savings: Mapping[str, float | None]) -> dict[datetime, float]:
    """The car's saving per UTC hour its sessions started in: a day's hourly bars put a charge's whole figure in the
    hour the charge started, as `car_days` puts it on the day it started."""
    out: dict[datetime, float] = {}
    for session in sessions:
        value = savings.get(session.id)
        if value is not None:
            hour = session.start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
            out[hour] = out.get(hour, 0.0) + value
    return out


# ------------------------------------------------------------------------------------------------ battery and heat


def battery_part(money: Any) -> tuple[float | None, float | None]:
    """The day's battery part and the share of the day SpotNav controlled the battery, from its money
    (`stored_value.DayMoney`); `(None, None)` without it, `(None, 0.0)` on a day of the battery's own control."""
    if money is None or money.quarters <= 0:
        return None, None
    share = min(1.0, max(0, getattr(money, "controlled", 0)) / money.quarters)
    return (money.arbitrage * share if share > 0 else None), share


def heat_part(record: Mapping[str, Any] | None) -> float | None:
    """The day's heat part from the heat shadow's day (`review`, `spotnav_min`); `None` before the review, without
    SpotNav's own lowering or in a day reviewed before `spotnav_min` was kept."""
    if not isinstance(record, Mapping):
        return None
    review, minutes = record.get("review"), record.get("spotnav_min")
    if not isinstance(review, Mapping) or not isinstance(minutes, (int, float)) or minutes <= 0:
        return None
    relay = review.get("relay")
    if not isinstance(relay, Mapping):
        return None
    hours, value = relay.get("hours"), relay.get("value")
    if not isinstance(hours, (int, float)) or not isinstance(value, (int, float)) or hours <= 0:
        return None
    return float(value) * min(1.0, minutes / 60.0 / hours)


def day_savings(
    days: Iterable[date],
    *,
    battery_money: Mapping[date, Any],
    heat_days: Mapping[date, Mapping[str, Any]],
    car: Mapping[date, float],
    car_since: date | None,
    today: date,
) -> dict[date, DaySaving]:
    """Each day's parts (see the module); days after `today` are left out."""
    out: dict[date, DaySaving] = {}
    for day in days:
        if day > today:
            continue
        battery, share = battery_part(battery_money.get(day))
        out[day] = DaySaving(
            battery=battery,
            car=None if car_since is None or day < car_since else car.get(day, 0.0),
            heat=heat_part(heat_days.get(day)),
            battery_share=share,
        )
    return out


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def saving_block(day: date, saving: DaySaving, car_since: date | None) -> dict[str, Any] | None:
    """The house card's `saving` (`SAVING_KEYS`); `None` when the day has no part at all and no battery share."""
    if saving.total is None and saving.battery_share is None:
        return None
    return {
        "date": day.isoformat(),
        "total": _r(saving.total),
        "battery": _r(saving.battery),
        "car": _r(saving.car),
        "heat": _r(saving.heat),
        "battery_share": None if saving.battery_share is None else round(saving.battery_share, 3),
        "car_since": None if car_since is None else car_since.isoformat(),
    }


def period_sums(savings: Mapping[date, DaySaving]) -> dict[str, Any] | None:
    """A period's sums: each part over the days that have it, with the days counted; `None` without any."""
    out: dict[str, Any] = {}
    for part in PARTS:
        values = [getattr(saving, part) for saving in savings.values() if getattr(saving, part) is not None]
        out[part] = _r(sum(values)) if values else None
        out[f"{part}_days"] = len(values)
    totals = [saving.total for saving in savings.values() if saving.total is not None]
    if not totals:
        return None
    return {"total": _r(sum(totals)), **out, "days": len(totals)}



def own_control(arbitrage: float | None, share: float | None, review: Any = None) -> tuple[float | None, float | None]:
    """A day's `(own, would)` (see the module) from its arbitrage and the share of it SpotNav controlled: what the
    battery earned in the share of the day it ran under its own control, and what SpotNav's plan would have earned
    there (`None` without the day's review); `(None, None)` on a day SpotNav controlled throughout or without the
    battery's day."""
    if arbitrage is None or share is None or share >= 1.0:
        return None, None
    own = arbitrage * (1.0 - share)
    if review is None:
        return own, None
    return own, own + (review.saving - review.actual_saving) * (1.0 - share)
