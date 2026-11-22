"""A plain water heater switched by SpotNav, pure (plan `plans/v3_zones_water_heater.md` section 3, the owner's
decisions of 2026-11-20): an immersion element on its own thermostat, no connectivity, behind a `switch` (a Shelly on a
contactor). The shell that reads and writes is `heat/water_heater_control.py`.

**Legionella first.** SpotNav cannot see the tank. All it can guarantee is that the element is *allowed* to run long
enough for its own thermostat to finish once a day, and is never kept off long; the temperature reached is the
thermostat's, so the person sets it to at least 60 °C (Boverket). The hard rules below involve no price, are checked at
every look and beat every plan and cap (`Rules.want`):

* **H1, full heat every day.** *Metered* (a power sensor on the switch's device): full heat is the element drawing at
  least `FULL_DRAW_W` while on and then under `FULL_IDLE_W` for `FULL_IDLE_FOR` in a row (its thermostat satisfied,
  `Rules.observe`). From `FULL_EVERY` after the last full heat (or with none known) the switch is held on, whatever the
  price, until full heat is seen again. *With a tank sensor*: full heat is the tank at `TANK_FULL_C` or more for
  `TANK_FULL_FOR`; under `TANK_LOW_C` the switch is on until it reads `TANK_BACK_C`. *Unmetered*: one contiguous
  `NIGHT_HOURS` window every night (the cheapest between 22:00 and 07:00; without prices 01:00-06:00), never shortened.
* **H2, never off long.** An off stretch of SpotNav's reaching `MAX_OFF` less `MAX_OFF_MARGIN` turns the switch on
  for at least `FORCED_ON`; the windows are planned with off gaps of at most `MAX_PLANNED_OFF`.
* **H3, fail-safe on.** No fresh plan or prices: on. The meter (metered) or the tank sensor not reading for
  `SENSOR_LOST_AFTER`: on until it reads. (The switch itself unreadable, every exit and the next start are the
  shell's: nothing written to a switch that does not read, on at its first readable look and at every exit.)
* **Give-up.** Full heat not seen for `GIVE_UP_AFTER` while control ran (metered or with a tank sensor): control
  stops with the switch on and one note until full heat is seen again (`Rules.gave_up`).

**The cheap windows** (the saving, inside the hard rules, `plan_windows`): *metered* with `WARM_DAYS` of history, the
day's need `E` is the `NEED_QUANTILE` of the last days' kWh times `NEED_SHARE`, the hours `ceil(E / kW) + 1` (at least
`MIN_HOURS`) split 60/40 into a night window ending by 05:00 and a day window ending by 17:00; otherwise (unmetered,
or under `WARM_DAYS` of history) the 5-hour night window ending by 07:00 and a 3-hour day window ending by 17:00. Each
is the cheapest block of its span, one without a tight hour where one fits. Away changes nothing. At most
`MAX_SWITCHINGS_DAY` planned switchings a day: the next off would pass it, so the switch stays on for the day.

**The ceiling** (`site/shed_order.py`): the site's shed order may pause a cheap window to the period's end
(`Rules.want(shed_until=...)`, `ceiling`), after every hard rule above, within the longest off stretch, and counted
among the day's switchings; never the unmetered night window or a gap filler.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Final
from zoneinfo import ZoneInfo

from ..core.parse import aware_instant

H: Final = timedelta(hours=1)

FULL_DRAW_W: Final = 500.0
FULL_IDLE_W: Final = 50.0
FULL_IDLE_FOR: Final = timedelta(minutes=15)
TANK_FULL_C: Final = 60.0
TANK_FULL_FOR: Final = timedelta(minutes=10)
TANK_LOW_C: Final = 40.0
TANK_BACK_C: Final = 45.0
#: From this long after the last full heat the switch is held on until the next (decision D3: 20-24 h).
FULL_EVERY: Final = timedelta(hours=20)
GIVE_UP_AFTER: Final = timedelta(hours=48)
#: The longest off stretch (D3), the margin the live rule keeps to it (a look is 30 s apart), and the planned one.
MAX_OFF: Final = timedelta(hours=8)
MAX_OFF_MARGIN: Final = timedelta(minutes=5)
MAX_PLANNED_OFF: Final = timedelta(hours=7)
FORCED_ON: Final = timedelta(hours=1)
SENSOR_LOST_AFTER: Final = timedelta(minutes=10)
MAX_SWITCHINGS_DAY: Final = 8

#: The unmetered night window (D3: 5 h) and its span, and the day window.
NIGHT_HOURS: Final = 5
NIGHT_START: Final = 22
NIGHT_END: Final = 7
METERED_NIGHT_END: Final = 5
DAY_START: Final = 7
DAY_END: Final = 17
DAY_HOURS: Final = 3
#: Without prices for the night, the unmetered window is these hours (local).
FALLBACK_NIGHT: Final = (1, 6)

MIN_HOURS: Final = 3
NEED_QUANTILE: Final = 0.8
NEED_SHARE: Final = 1.2
NIGHT_SHARE: Final = 0.6
HISTORY_DAYS: Final = 14
WARM_DAYS: Final = 3

MODE_METERED: Final = "metered"
MODE_UNMETERED: Final = "unmetered"
MODE_TANK: Final = "tank"

#: A window's reason (the card words them).
WINDOW_CHEAP: Final = "cheap"
WINDOW_FULL_HEAT: Final = "full_heat"
WINDOW_MAX_OFF: Final = "max_off"

#: Why the switch is on or off now.
WANT_FORCED: Final = "max_off"
WANT_SENSOR_LOST: Final = "sensor_lost"
WANT_NO_PLAN: Final = "no_plan"
WANT_FULL_HEAT: Final = "full_heat"
WANT_TANK_LOW: Final = "tank_low"
WANT_WRITE_CAP: Final = "write_cap"
WANT_OFF: Final = "planned_off"
#: Paused for the ceiling (`site/shed_order.py`): only in a cheap window, after every hard rule.
WANT_SHED: Final = "ceiling"


def mode_of(metered: bool, tank: bool) -> str:
    """How full heat is seen: a tank sensor, else a power meter, else not at all (the fixed night window)."""
    return MODE_TANK if tank else MODE_METERED if metered else MODE_UNMETERED


@dataclass(frozen=True, slots=True)
class Window:
    start: datetime
    end: datetime
    reason: str
    #: Which window it is (`night:<date>`, `day:<date>`, `gap:<start>`): a started one is never planned again.
    key: str
    kwh: float | None = None

    def covers(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "reason": self.reason,
            "key": self.key,
            "kwh": None if self.kwh is None else round(self.kwh, 3),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Window | None:
        if not isinstance(raw, Mapping):
            return None
        start, end = aware_instant(raw.get("start")), aware_instant(raw.get("end"))
        reason, key, kwh = raw.get("reason"), raw.get("key"), raw.get("kwh")
        if start is None or end is None or end <= start or not isinstance(reason, str) or not isinstance(key, str):
            return None
        return cls(start, end, reason, key, float(kwh) if isinstance(kwh, (int, float)) and not isinstance(kwh, bool) else None)


# ------------------------------------------------------------------------------------------------ the windows


@dataclass(frozen=True, slots=True)
class Sizes:
    night_hours: int
    day_hours: int
    #: The local hour the night window ends by.
    night_end: int
    #: The night window is H1 itself (unmetered: never shortened, planned even without prices).
    hard_night: bool
    night_kwh: float | None = None
    day_kwh: float | None = None


def need_kwh(daily_kwh: Sequence[float]) -> float | None:
    """The day's need: the `NEED_QUANTILE` of the days' kWh times `NEED_SHARE`, `None` under `WARM_DAYS` days."""
    days = [kwh for kwh in daily_kwh if math.isfinite(kwh) and kwh >= 0][-HISTORY_DAYS:]
    if len(days) < WARM_DAYS:
        return None
    ordered = sorted(days)
    quantile = ordered[min(len(ordered) - 1, math.ceil(len(ordered) * NEED_QUANTILE) - 1)]
    return quantile * NEED_SHARE


def sizes(mode: str, daily_kwh: Sequence[float], kw: float | None) -> Sizes:
    """The windows' lengths (see the module)."""
    need = need_kwh(daily_kwh) if mode != MODE_UNMETERED else None
    if need is None or kw is None or kw <= 0:
        return Sizes(NIGHT_HOURS, DAY_HOURS, NIGHT_END, mode == MODE_UNMETERED)
    hours = max(MIN_HOURS, math.ceil(need / kw) + 1)
    night = max(1, math.ceil(hours * NIGHT_SHARE))
    day = max(1, hours - night)
    return Sizes(night, day, METERED_NIGHT_END, False, need * night / (night + day), need * day / (night + day))


def _local(day: date, hour: int, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=zone).astimezone(timezone.utc)


def _hours(start: datetime, end: datetime) -> list[datetime]:
    out: list[datetime] = []
    moment = start
    while moment < end:
        out.append(moment)
        moment += H
    return out


def cheapest_block(
    hours: Sequence[datetime], length: int, prices: Mapping[datetime, float], tight: Iterable[datetime] = ()
) -> list[datetime] | None:
    """The cheapest `length` hours in a row among `hours` (consecutive UTC hour starts), all priced; one without a tight
    hour where one fits. `None` when no block is priced."""
    if length <= 0 or len(hours) < length:
        return None
    blocked = set(tight)
    blocks = [list(hours[i : i + length]) for i in range(len(hours) - length + 1)]
    priced = [block for block in blocks if all(hour in prices for hour in block)]
    if not priced:
        return None
    free = [block for block in priced if not blocked.intersection(block)]
    return min(free or priced, key=lambda block: (sum(prices[hour] for hour in block), block[0]))


def _block_window(block: Sequence[datetime], reason: str, key: str, kwh: float | None) -> Window:
    return Window(block[0], block[-1] + H, reason, key, kwh)


def plan_windows(
    now: datetime,
    zone: ZoneInfo,
    prices: Mapping[datetime, float],
    size: Sizes,
    *,
    tight: Iterable[datetime] = (),
    kept: Sequence[Window] = (),
    off_since: datetime | None = None,
) -> list[Window]:
    """The windows from the last night on (see the module): `kept` are windows already started (or done) and are never
    planned again; a night or day window whose span has begun is chosen among the hours still ahead. The unmetered
    night window (`hard_night`) is always planned: without prices for its span at `FALLBACK_NIGHT`, and where too few
    hours of its span are left, from the running hour on for its full length. Off gaps longer than `MAX_PLANNED_OFF`
    from `off_since` (else now) to the last window are filled with one-hour windows (`WINDOW_MAX_OFF`)."""
    tight = frozenset(tight)
    current = now.replace(minute=0, second=0, microsecond=0)
    out: dict[str, Window] = {window.key: window for window in kept if window.start <= now}
    today = now.astimezone(zone).date()
    for day in (today, today + timedelta(days=1)):
        night_key, day_key = f"night:{day.isoformat()}", f"day:{day.isoformat()}"
        night_span = _hours(_local(day - timedelta(days=1), NIGHT_START, zone), _local(day, size.night_end, zone))
        if night_key not in out and night_span and night_span[-1] + H > now:
            ahead = [hour for hour in night_span if hour >= current]
            block = cheapest_block(ahead, size.night_hours, prices, tight)
            reason = WINDOW_FULL_HEAT if size.hard_night else WINDOW_CHEAP
            if block is not None:
                out[night_key] = _block_window(block, reason, night_key, size.night_kwh)
            elif size.hard_night:
                start = _local(day, FALLBACK_NIGHT[0], zone)
                if start < current:
                    start = current
                out[night_key] = Window(start, start + size.night_hours * H, reason, night_key)
        day_span = _hours(_local(day, DAY_START, zone), _local(day, DAY_END, zone))
        if day_key not in out and day_span and day_span[-1] + H > now:
            ahead = [hour for hour in day_span if hour >= current]
            block = cheapest_block(ahead, size.day_hours, prices, tight)
            if block is not None:
                out[day_key] = _block_window(block, WINDOW_CHEAP, day_key, size.day_kwh)
    windows = sorted(out.values(), key=lambda window: window.start)
    windows += fill_gaps(windows, now if off_since is None else min(off_since, now), prices, tight, not_before=current)
    return sorted(windows, key=lambda window: window.start)


def fill_gaps(
    windows: Sequence[Window],
    start: datetime,
    prices: Mapping[datetime, float],
    tight: Iterable[datetime] = (),
    *,
    not_before: datetime | None = None,
) -> list[Window]:
    """One-hour windows so that no off gap from `start` (an off stretch's beginning) to the last window's end is longer
    than `MAX_PLANNED_OFF`: each the cheapest whole hour (one not tight first) that keeps the gaps before and after
    within it, else the latest that keeps the one before; never before `not_before` (the running hour)."""
    tight = frozenset(tight)
    added: list[Window] = []
    cursor = start
    spans = sorted((window.start, window.end) for window in windows if window.end > start)
    for begin, end in spans:
        while begin - cursor > MAX_PLANNED_OFF:
            latest = cursor + MAX_PLANNED_OFF
            # Where one window cannot keep both gaps within the limit, any hour that keeps the one before will do.
            earliest = begin - MAX_PLANNED_OFF - H
            earliest = cursor if earliest > latest else max(cursor, earliest)
            first = earliest.replace(minute=0, second=0, microsecond=0)
            if first < earliest:
                first += H
            options = _hours(first, latest + timedelta(seconds=1)) if first <= latest else []
            if not_before is not None:
                options = [hour for hour in options if hour >= not_before]
                if not options and latest < not_before + H:
                    # Already as long as it may be: the running hour.
                    options = [not_before]
            if not options:
                # The gap is longer than two stretches: the latest whole hour that keeps the one before.
                last = latest.replace(minute=0, second=0, microsecond=0)
                options = [last if last > cursor else latest]
            priced = [hour for hour in options if hour in prices]
            if priced:
                free = [hour for hour in priced if hour not in tight]
                pick = min(free or priced, key=lambda hour: (prices[hour], hour))
            else:
                pick = options[-1]
            if not_before is not None and pick < not_before:
                pick = not_before
            added.append(Window(pick, pick + H, WINDOW_MAX_OFF, f"gap:{pick.isoformat()}"))
            cursor = pick + H
        cursor = max(cursor, end)
    return added


def forecast_delta(windows: Sequence[Window], now: datetime, zone: ZoneInfo) -> dict[datetime, float]:
    """The windows' expected energy on their hours, and the same energy taken evenly from the other hours of their
    local day (the house forecast already holds the heater's mean): kW per UTC hour start, from the running hour on."""
    current = now.replace(minute=0, second=0, microsecond=0)
    out: dict[datetime, float] = {}
    by_day: dict[date, list[Window]] = {}
    for window in windows:
        if window.kwh is None or window.kwh <= 0 or window.end <= current:
            continue
        by_day.setdefault(window.start.astimezone(zone).date(), []).append(window)
    for day, items in by_day.items():
        hours = _hours(_local(day, 0, zone), _local(day + timedelta(days=1), 0, zone))
        on: dict[datetime, float] = {}
        for window in items:
            span = _hours(window.start.replace(minute=0, second=0, microsecond=0), window.end)
            for hour in span:
                on[hour] = on.get(hour, 0.0) + window.kwh / len(span)  # type: ignore[operator]
        total = sum(on.values())
        others = [hour for hour in hours if hour not in on]
        for hour, kw in on.items():
            out[hour] = out.get(hour, 0.0) + kw
        for hour in others:
            out[hour] = out.get(hour, 0.0) - total / len(others)
    return {hour: round(kw, 4) for hour, kw in sorted(out.items()) if hour >= current and abs(kw) > 1e-9}


# ------------------------------------------------------------------------------------------------ the hard rules


@dataclass(slots=True)
class Kept:
    """What the rules keep across a restart (the shell's store)."""

    last_full: datetime | None = None
    #: Since when control has run (the give-up's reference while no full heat has been seen since).
    started: datetime | None = None
    #: SpotNav holds the switch off since.
    off_since: datetime | None = None
    #: H2: held on until.
    forced_until: datetime | None = None
    tank_low: bool = False
    #: Control stopped for want of full heat (the note), until full heat is seen or the opt-in is turned off and on.
    stopped: bool = False
    #: Planned switchings per local day (ISO date).
    switchings: dict[str, int] = field(default_factory=dict)
    windows: list[Window] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        iso = lambda moment: None if moment is None else moment.isoformat()  # noqa: E731
        return {
            "last_full": iso(self.last_full),
            "started": iso(self.started),
            "off_since": iso(self.off_since),
            "forced_until": iso(self.forced_until),
            "tank_low": self.tank_low,
            "stopped": self.stopped,
            "switchings": dict(self.switchings),
            "windows": [window.as_dict() for window in self.windows],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Kept:
        if not isinstance(raw, Mapping):
            return cls()
        switchings = raw.get("switchings")
        windows = [Window.from_dict(item) for item in raw.get("windows") or []] if isinstance(raw.get("windows"), list) else []
        return cls(
            last_full=aware_instant(raw.get("last_full")),
            started=aware_instant(raw.get("started")),
            off_since=aware_instant(raw.get("off_since")),
            forced_until=aware_instant(raw.get("forced_until")),
            tank_low=raw.get("tank_low") is True,
            stopped=raw.get("stopped") is True,
            switchings={day: count for day, count in switchings.items() if isinstance(day, str) and isinstance(count, int)}
            if isinstance(switchings, Mapping)
            else {},
            windows=[window for window in windows if window is not None],
        )


@dataclass(frozen=True, slots=True)
class Want:
    on: bool
    reason: str
    until: datetime | None = None


class Rules:
    """The hard rules and the windows' verdict for one water heater (see the module). Pure: the shell feeds each
    look's readings and tells it each switching it made."""

    def __init__(self, kept: Kept | None = None) -> None:
        self.kept = kept or Kept()
        #: Full-heat detection (memory only: a restart starts it again, which only holds the switch on longer).
        self.drew = False
        self.idle_since: datetime | None = None
        self.tank_since: datetime | None = None
        self.meter_lost_since: datetime | None = None
        self.tank_lost_since: datetime | None = None

    # ---- seen at every look, in control or not

    def observe(
        self, now: datetime, *, on: bool | None, power_w: float | None, metered: bool, tank_c: float | None, tank: bool
    ) -> bool:
        """Step full-heat detection and the sensors' loss; `True` when full heat is seen now."""
        kept = self.kept
        self.meter_lost_since = (self.meter_lost_since or now) if metered and power_w is None else None
        self.tank_lost_since = (self.tank_lost_since or now) if tank and tank_c is None else None
        if tank and tank_c is not None:
            if tank_c < TANK_LOW_C:
                kept.tank_low = True
            elif tank_c >= TANK_BACK_C:
                kept.tank_low = False
        elif not tank:
            kept.tank_low = False
        full = False
        if tank:
            if tank_c is not None and tank_c >= TANK_FULL_C:
                self.tank_since = self.tank_since or now
                if now - self.tank_since >= TANK_FULL_FOR:
                    full = True
            else:
                self.tank_since = None
        elif metered:
            if on is not True or power_w is None:
                self.idle_since = None
                if on is False:
                    self.drew = False
            elif power_w >= FULL_DRAW_W:
                self.drew, self.idle_since = True, None
            elif power_w < FULL_IDLE_W and self.drew:
                self.idle_since = self.idle_since or now
                if now - self.idle_since >= FULL_IDLE_FOR:
                    full = True
                    self.drew, self.idle_since = False, None
            else:
                self.idle_since = None
        if full:
            kept.last_full = now
            kept.stopped = False
        return full

    # ---- control

    def start(self, now: datetime) -> None:
        """Control runs from now (the give-up's reference)."""
        if self.kept.started is None:
            self.kept.started = now

    def stop(self) -> None:
        """Control no longer runs: nothing of SpotNav's holds."""
        self.kept.started = None
        self.kept.off_since = None
        self.kept.forced_until = None

    def gave_up(self, now: datetime, mode: str) -> bool:
        """Whether full heat has not been seen for `GIVE_UP_AFTER` while control ran (metered or tank): control stops
        (`stopped`) until full heat is seen again."""
        kept = self.kept
        if mode == MODE_UNMETERED or kept.started is None:
            return kept.stopped
        reference = kept.started if kept.last_full is None else max(kept.last_full, kept.started)
        if now - reference >= GIVE_UP_AFTER:
            kept.stopped = True
        return kept.stopped

    def want(
        self,
        now: datetime,
        *,
        mode: str,
        windows: Sequence[Window],
        plan_fresh: bool,
        day: str,
        shed_until: datetime | None = None,
    ) -> Want:
        """Whether the switch is to be on now while control runs, and why (see the module); hard rules first.

        `shed_until`: the site's shed order pauses it for the ceiling until then (`site/shed_order.py`). Only a cheap
        window is paused, after every hard rule above (an unmetered night window and the gap fillers are hard rules
        too), within the longest off stretch and the day's switchings (an off it could not end with an on in the day's
        cap leaves it on)."""
        kept = self.kept
        if kept.forced_until is not None and now < kept.forced_until:
            return Want(True, WANT_FORCED, kept.forced_until)
        kept.forced_until = None
        if self.meter_lost_since is not None and mode == MODE_METERED and now - self.meter_lost_since >= SENSOR_LOST_AFTER:
            return Want(True, WANT_SENSOR_LOST)
        if self.tank_lost_since is not None and mode == MODE_TANK and now - self.tank_lost_since >= SENSOR_LOST_AFTER:
            return Want(True, WANT_SENSOR_LOST)
        if not plan_fresh:
            return Want(True, WANT_NO_PLAN)
        if mode != MODE_UNMETERED and (kept.last_full is None or now - kept.last_full >= FULL_EVERY):
            return Want(True, WANT_FULL_HEAT)
        if mode == MODE_TANK and kept.tank_low:
            return Want(True, WANT_TANK_LOW)
        if kept.off_since is not None and now - kept.off_since >= MAX_OFF - MAX_OFF_MARGIN:
            kept.forced_until = now + FORCED_ON
            return Want(True, WANT_FORCED, kept.forced_until)
        window = next((item for item in windows if item.covers(now)), None)
        if window is not None:
            if shed_until is not None and now < shed_until and window.reason == WINDOW_CHEAP:
                if kept.off_since is None and kept.switchings.get(day, 0) + 2 > MAX_SWITCHINGS_DAY:
                    return Want(True, WANT_WRITE_CAP)
                latest = (kept.off_since or now) + MAX_OFF - MAX_OFF_MARGIN
                return Want(False, WANT_SHED, min(shed_until, latest))
            return Want(True, window.reason, window.end)
        if kept.off_since is None and kept.switchings.get(day, 0) + 2 > MAX_SWITCHINGS_DAY:
            return Want(True, WANT_WRITE_CAP)
        following = min((item.start for item in windows if item.start > now), default=None)
        latest = (kept.off_since or now) + MAX_OFF - MAX_OFF_MARGIN
        return Want(False, WANT_OFF, latest if following is None else min(following, latest))

    def would_shed(self, now: datetime, *, mode: str, windows: Sequence[Window], plan_fresh: bool, day: str) -> bool:
        """Whether a pause for the ceiling asked for now would be taken (`want` with `shed_until` just ahead): every
        hard rule, the cheap window, the day's switchings and the longest off stretch. Changes nothing kept."""
        trial = Rules(Kept.from_dict(self.kept.as_dict()))
        trial.meter_lost_since, trial.tank_lost_since = self.meter_lost_since, self.tank_lost_since
        want = trial.want(now, mode=mode, windows=windows, plan_fresh=plan_fresh, day=day, shed_until=now + timedelta(seconds=1))
        return want.reason == WANT_SHED

    def switched(self, now: datetime, on: bool, *, day: str, planned: bool) -> None:
        """A switching of SpotNav's that read back: the off stretch, and the day's planned switchings."""
        kept = self.kept
        if on:
            kept.off_since = None
        elif kept.off_since is None:
            kept.off_since = now
        if planned:
            kept.switchings[day] = kept.switchings.get(day, 0) + 1

    def trim(self, now: datetime, first_day: str) -> None:
        kept = self.kept
        kept.switchings = {day: count for day, count in kept.switchings.items() if day >= first_day}
        kept.windows = [window for window in kept.windows if window.end > now - timedelta(days=1)]


def daily_kwh(hourly: Mapping[datetime, float], zone: ZoneInfo, now: datetime) -> list[float]:
    """The finished local days' kWh from hourly kWh (UTC hour starts), oldest first; a day with under 20 hours of rows
    is left out."""
    days: dict[date, list[float]] = {}
    today = now.astimezone(zone).date()
    for start, kwh in hourly.items():
        day = start.astimezone(zone).date()
        if day < today and math.isfinite(kwh):
            days.setdefault(day, []).append(kwh)
    return [sum(values) for day, values in sorted(days.items()) if len(values) >= 20]


def element_kw(power_w: Iterable[float]) -> float | None:
    """The element's power: the median of the readings while it drew (kW)."""
    drawing = [value for value in power_w if value >= FULL_DRAW_W]
    return None if not drawing else statistics.median(drawing) / 1000.0
