"""The site's hour tracker: each clock hour's mean net import, the month's hours, and the running hour.

A demand charge bills hourly means of net import (`planning/demand_charge.py`), and an import target is one too.
Only a site has a meter, so the site measures them, for every component on its electricity price and for its own
import target; this module only measures and never decides anything a charger is told.

* **Input**: the site's net grid power (W, positive = import) on solar's basis (`execution/solar_execution.py:
  site_net_grid_w`: a direct site's total grid power, a derived site's signed power summed over its phases, each
  only while live), and what the site's chargers draw (W, `site_car_w`, `None` when one cannot be read).
* **Integration**: `import_wh += max(0, grid_w) x dt` over each local clock hour, keyed by its start as a UTC
  instant (`hour_start`), so the repeated autumn hour is two hours, as on the meter. Each reading is held until the
  next observation, at most `HOLD_MAX_S`; time without a usable reading is a gap. The house's share is integrated
  beside it as `max(0, grid_w - car_w)` (what the house would have imported without the cars), for a later floor.
  A home battery's discharge (`discharge_w`, W, positive while it discharges) is integrated beside it over its own
  measured time, for an import target under `battery_first` (`site/import_target.py`); it never enters a peak.
* **An hour's mean** is its energy over the time it was measured. An hour with more than `GAP_MAX_S` of gaps is
  `incomplete`: kept and shown, but it never raises a peak. An hour never measured at all is not kept.
* **Restart mid-hour**: the running hour is kept (`RUNNING_SAVE_DELAY_S`, at most once a minute); after a restart
  the time since its last write is a gap, so a restart longer than `GAP_MAX_S` makes that hour incomplete.
* **Month**: the hours of the running month on the area's clock, and those of the month before (for the floor a
  later release uses). At the first hour of a new month the month's hours become the previous month's.

* **Import target**: each hour carries whether the site's import target held a charger's current down or paused a
  charger in it (`TargetMark`, from the site's last decision), for the power view only.
* **Two years of days**: beside the hours, a summary per day of each month is kept for two years
  (`site/power_history.py`), refiled from the hours when an hour completes.
* **Back-fill** (`SiteDemand.async_backfill`): once, when the site first measures (or when the power view finds no
  earlier month), the hours before SpotNav's first measured one are read from Home Assistant's hourly statistics
  (`site/hour_statistics.py`, `PERIOD_DAYS` back), marked `imported`: the running month's and the month before's as
  hours, older months as day summaries where none are kept. An hour SpotNav measured is never overwritten (nothing
  at or after its first measured hour is filled), and an imported hour never lifts the import target.

`HourTracker` is pure (the clock is passed in); `SiteDemand` keeps it in the site's own three stores (the month's
hours and the day summaries, written when an hour completes; the running hour, at most once a minute). Storage is
untrusted: a malformed record is dropped, never raised on.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..debug_day import DayStore
from ..planning.auto_settings import DemandComponent
from ..planning.demand_charge import component_month, hour_start, HourMean, month_of
from .power_history import months_before, power_month, PowerArchive

_LOGGER = logging.getLogger(__name__)

#: An hour with more time than this without a usable reading is incomplete: it never raises a peak.
GAP_MAX_S: Final = 600.0
#: The longest a reading is held between two observations; beyond it the time is a gap (Home Assistant stopped).
HOLD_MAX_S: Final = 120.0
#: The site observes at most this often (it recomputes on every meter report, and every 30 s at least).
OBSERVE_MIN_S: Final = 5.0
#: How long a change of the running hour waits for its write.
RUNNING_SAVE_DELAY_S: Final = 60.0
#: How long a completed hour waits for its write (the month's hours change once an hour).
MONTH_SAVE_DELAY_S: Final = 5.0
#: After this long without an observation nothing is filled in hour by hour: the tracker starts again.
RESTART_AFTER_S: Final = 40 * 24 * 3600.0
HOUR_S: Final = 3600.0
#: A back-fill that filed nothing may be asked for again (by the power view) after this long.
BACKFILL_RETRY_S: Final = 24 * 3600.0
BACKFILL_DONE: Final = "done"
BACKFILL_NO_STATISTICS: Final = "no_statistics"
BACKFILL_FAILED: Final = "failed"


def month_store_key(entry_id: str) -> str:
    return f"{DOMAIN}.demand.{entry_id}"


def running_store_key(entry_id: str) -> str:
    return f"{DOMAIN}.demand_hour.{entry_id}"


def days_store_key(entry_id: str) -> str:
    return f"{DOMAIN}.demand_days.{entry_id}"


@dataclass(frozen=True, slots=True)
class TargetMark:
    """What the site's import target did in the hour starting at `hour_start` (UTC): held a charger's current down,
    or paused a charger. Recorded beside the hour for the power view; it decides nothing."""

    hour_start: datetime
    held: bool
    paused: bool


@dataclass(slots=True)
class RunningHour:
    """The clock hour being measured: its start (UTC), energy and measured time so far, and the last reading."""

    start: datetime
    import_wh: float = 0.0
    covered_s: float = 0.0
    house_wh: float = 0.0
    house_covered_s: float = 0.0
    #: A home battery's discharge over the time it was read with the grid (`battery_first`'s import target only).
    discharge_wh: float = 0.0
    discharge_covered_s: float = 0.0
    #: When the last observation was made, and what it read (`None`: no usable reading, or a restart since).
    last_at: datetime | None = None
    last_grid_w: float | None = None
    last_car_w: float | None = None
    last_discharge_w: float | None = None
    #: The site's import target held a charger's current down, or paused a charger, at some point in this hour.
    held: bool = False
    paused: bool = False

    @property
    def gap_s(self) -> float:
        """Time of the hour so far without a usable reading."""
        if self.last_at is None:
            return 0.0
        return max(0.0, (self.last_at - self.start).total_seconds() - self.covered_s)

    def mean_kw(self) -> float | None:
        """The mean net import over the time measured so far, `None` before any."""
        return None if self.covered_s <= 0 else self.import_wh / self.covered_s * 3.6

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "import_wh": round(self.import_wh, 3),
            "covered_s": round(self.covered_s, 3),
            "house_wh": round(self.house_wh, 3),
            "house_covered_s": round(self.house_covered_s, 3),
            "discharge_wh": round(self.discharge_wh, 3),
            "discharge_covered_s": round(self.discharge_covered_s, 3),
            "last_at": None if self.last_at is None else self.last_at.isoformat(),
            "held": self.held,
            "paused": self.paused,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> RunningHour | None:
        """A stored running hour, its last reading unknown (a restart since), or `None` when it is not one."""
        if not isinstance(raw, dict):
            return None
        start = _instant(raw.get("start"))
        last_at = _instant(raw.get("last_at"))
        numbers = [raw.get(key) for key in ("import_wh", "covered_s", "house_wh", "house_covered_s")]
        if start is None or not all(_number(value) and value >= 0 for value in numbers):
            return None
        if any(value > HOUR_S for value in (numbers[1], numbers[3])):
            return None
        # Kept since the import target holds: a record without them (or with a bad one) counts no discharge.
        discharge = [raw.get(key, 0.0) for key in ("discharge_wh", "discharge_covered_s")]
        if not all(_number(value) and value >= 0 for value in discharge) or discharge[1] > HOUR_S:
            discharge = [0.0, 0.0]
        return cls(
            start=start,
            import_wh=float(numbers[0]),
            covered_s=float(numbers[1]),
            house_wh=float(numbers[2]),
            house_covered_s=float(numbers[3]),
            discharge_wh=float(discharge[0]),
            discharge_covered_s=float(discharge[1]),
            last_at=last_at,
            held=raw.get("held") is True,
            paused=raw.get("paused") is True,
        )


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _instant(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return None if moment.tzinfo is None else moment.astimezone(timezone.utc)


def hour_row(hour: HourMean) -> list[Any]:
    """A completed hour as it is kept and shown in the diagnostics: `[start, kW, house kW, incomplete, held,
    paused, floor kW]`, the flags as 0 or 1; `kW` is the mean over the time measured, `floor kW` the import over
    the whole hour with any gap counted as none (`null` for an hour kept before it was). An hour read from Home
    Assistant's statistics (`imported`) has an eighth element, `1`; a measured one has seven."""
    row = [
        hour.start.isoformat(),
        round(hour.kw, 3),
        None if hour.house_kw is None else round(hour.house_kw, 3),
        1 if hour.incomplete else 0,
        1 if hour.held else 0,
        1 if hour.paused else 0,
        None if hour.floor_kw is None else round(hour.floor_kw, 3),
    ]
    if hour.imported:
        row.append(1)
    return row


def _hour_from_row(raw: Any) -> HourMean | None:
    """A kept hour, or `None`. A row of four (kept before the import target's hours were marked) is unmarked; one of
    six (kept before the hour's floor was) has none; one of eight was read from Home Assistant's statistics."""
    if not isinstance(raw, list) or len(raw) not in (4, 6, 7, 8):
        return None
    imported = raw[7] if len(raw) == 8 else 0
    if isinstance(imported, bool) or imported != 1 and len(raw) == 8:
        return None
    start = _instant(raw[0])
    kw, house, incomplete = raw[1], raw[2], raw[3]
    held, paused = (raw[4], raw[5]) if len(raw) >= 6 else (0, 0)
    floor = raw[6] if len(raw) >= 7 else None
    if floor is not None and (not _number(floor) or floor < 0):
        return None
    if start is None or not _number(kw) or kw < 0 or any(flag not in (0, 1) for flag in (incomplete, held, paused)):
        return None
    if any(isinstance(flag, bool) for flag in (incomplete, held, paused)):
        return None
    if house is not None and (not _number(house) or house < 0):
        return None
    return HourMean(
        start=start,
        kw=float(kw),
        house_kw=None if house is None else float(house),
        incomplete=bool(incomplete),
        held=bool(held),
        paused=bool(paused),
        floor_kw=None if floor is None else float(floor),
        imported=imported == 1,
    )


def _month_text(month: tuple[int, int] | None) -> str | None:
    return None if month is None else f"{month[0]:04d}-{month[1]:02d}"


def _month_from(raw: Any) -> tuple[int, int] | None:
    if not isinstance(raw, str) or len(raw) != 7 or raw[4] != "-":
        return None
    try:
        year, month = int(raw[:4]), int(raw[5:])
    except ValueError:
        return None
    return (year, month) if 1 <= month <= 12 else None


def _following(month: tuple[int, int]) -> tuple[int, int]:
    return (month[0] + 1, 1) if month[1] == 12 else (month[0], month[1] + 1)


@dataclass(slots=True)
class HourTracker:
    """The hour tracker (see the module): pure, the clock passed to `observe`."""

    tz: str
    month: tuple[int, int] | None = None
    hours: list[HourMean] = field(default_factory=list)
    previous_month: tuple[int, int] | None = None
    previous: list[HourMean] = field(default_factory=list)
    running: RunningHour | None = None

    def observe(
        self,
        now: datetime,
        grid_w: float | None,
        car_w: float | None,
        discharge_w: float | None = None,
        target: TargetMark | None = None,
    ) -> bool:
        """Take a reading at `now`: the time since the last one is integrated at the last reading (held at most
        `HOLD_MAX_S`), completed hours are kept, and from now on `grid_w`/`car_w`/`discharge_w` are held. `target`
        marks the running hour when it is the hour the import target held or paused a charger in. Returns whether
        the month's hours changed (an hour completed, or the month rolled)."""
        now = now.astimezone(timezone.utc)
        changed = False
        running = self.running
        if running is None or running.last_at is None or now < running.last_at:
            # The first reading, a running hour restored without a moment, or a clock that went back: start here.
            if running is None or hour_start(now, self.tz) != running.start:
                running = RunningHour(start=hour_start(now, self.tz))
        elif (now - running.last_at).total_seconds() > RESTART_AFTER_S:
            running = RunningHour(start=hour_start(now, self.tz))
        else:
            held_until = running.last_at + timedelta(seconds=HOLD_MAX_S)
            moment = running.last_at
            while True:
                end_of_hour = running.start + timedelta(hours=1)
                end = min(now, end_of_hour)
                self._integrate(running, moment, end, held_until)
                if end < end_of_hour:
                    break
                changed |= self._complete(running)
                moment = end_of_hour
                running = RunningHour(
                    start=end_of_hour,
                    last_at=end_of_hour,
                    last_grid_w=running.last_grid_w,
                    last_car_w=running.last_car_w,
                    last_discharge_w=running.last_discharge_w,
                )
        running.last_at = now
        running.last_grid_w = None if grid_w is None or not _number(grid_w) else float(grid_w)
        running.last_car_w = None if car_w is None or not _number(car_w) else float(car_w)
        running.last_discharge_w = None if discharge_w is None or not _number(discharge_w) else float(discharge_w)
        if target is not None and target.hour_start == running.start:
            running.held |= target.held
            running.paused |= target.paused
        self.running = running
        changed |= self._roll_to(month_of(running.start, self.tz))
        return changed

    @staticmethod
    def _integrate(running: RunningHour, start: datetime, end: datetime, held_until: datetime) -> None:
        """Add the time from `start` to `end` at the held reading; the part after `held_until` is a gap."""
        if running.last_grid_w is None:
            return
        seconds = (min(end, held_until) - start).total_seconds()
        if seconds <= 0:
            return
        running.import_wh += max(0.0, running.last_grid_w) * seconds / HOUR_S
        running.covered_s += seconds
        if running.last_car_w is not None:
            running.house_wh += max(0.0, running.last_grid_w - running.last_car_w) * seconds / HOUR_S
            running.house_covered_s += seconds
        if running.last_discharge_w is not None:
            running.discharge_wh += max(0.0, running.last_discharge_w) * seconds / HOUR_S
            running.discharge_covered_s += seconds

    def _complete(self, running: RunningHour) -> bool:
        """File a completed hour under its month; an hour never measured is not kept."""
        if running.covered_s <= 0:
            return False
        house = (
            running.house_wh / running.house_covered_s * 3.6
            if running.house_covered_s > 0 and HOUR_S - running.house_covered_s <= GAP_MAX_S
            else None
        )
        hour = HourMean(
            start=running.start,
            kw=running.import_wh / running.covered_s * 3.6,
            house_kw=house,
            incomplete=HOUR_S - running.covered_s > GAP_MAX_S,
            held=running.held,
            paused=running.paused,
            floor_kw=running.import_wh / HOUR_S * 3.6,
        )
        month = month_of(hour.start, self.tz)
        changed = self._roll_to(month)
        if month == self.month:
            self.hours.append(hour)
            return True
        if month == self.previous_month:
            self.previous.append(hour)
            return True
        return changed

    def _roll_to(self, month: tuple[int, int]) -> bool:
        """Make `month` the running month when it is later; the month before it keeps its hours."""
        if self.month is not None and month <= self.month:
            return False
        if self.month is not None and _following(self.month) == month:
            self.previous_month, self.previous = self.month, self.hours
        else:
            self.previous_month, self.previous = None, []
        self.month, self.hours = month, []
        return True

    def month_hours(self, now: datetime) -> Sequence[HourMean]:
        """The completed hours of the month `now` falls in (none yet when the tracker has not reached it)."""
        return self.hours if self.month == month_of(hour_start(now, self.tz), self.tz) else ()

    def running_hour(self, now: datetime) -> RunningHour | None:
        """The hour being measured, while it is the hour `now` falls in."""
        running = self.running
        return running if running is not None and running.start == hour_start(now, self.tz) else None

    # ---- storage

    def month_dict(self) -> dict[str, Any]:
        return {
            "tz": self.tz,
            "month": _month_text(self.month),
            "hours": [hour_row(hour) for hour in self.hours],
            "previous_month": _month_text(self.previous_month),
            "previous": [hour_row(hour) for hour in self.previous],
        }

    def restore(self, month_data: Any, running_data: Any) -> None:
        """Take what the stores kept; a malformed part is dropped. The running hour's last reading is unknown."""
        if isinstance(month_data, dict):
            month = _month_from(month_data.get("month"))
            if month is not None:
                self.month = month
                self.hours = _rows(month_data.get("hours"))
                previous = _month_from(month_data.get("previous_month"))
                if previous is not None:
                    self.previous_month = previous
                    self.previous = _rows(month_data.get("previous"))
        running = RunningHour.from_dict(running_data)
        if running is not None:
            self.running = running


def _rows(raw: Any) -> list[HourMean]:
    if not isinstance(raw, list):
        return []
    hours = [hour for hour in (_hour_from_row(item) for item in raw) if hour is not None]
    unique: dict[datetime, HourMean] = {hour.start: hour for hour in hours}
    return sorted(unique.values(), key=lambda hour: hour.start)


# ------------------------------------------------------------------------------------------------ Home Assistant


class SiteDemand:
    """One site's hour tracker, kept in the site's two stores (see the module). Never raises into the site."""

    def __init__(
        self,
        hass: HomeAssistant | None,
        entry_id: str | None,
        tz: str,
        *,
        now: Callable[[], datetime] = dt_util.utcnow,
    ) -> None:
        self.tracker = HourTracker(tz=tz)
        self.archive = PowerArchive()
        self._now = now
        self._last_observed: datetime | None = None
        #: How many readings were taken: a reader that builds from the tracker builds again only when it moves.
        self.readings = 0
        self._loaded = hass is None or entry_id is None
        self._month_store: DayStore | None = None
        self._running_store: DayStore | None = None
        self._days_store: DayStore | None = None
        self._backfill_running = False
        if hass is not None and entry_id is not None:
            self._month_store = DayStore(
                hass, month_store_key(entry_id), self.tracker.month_dict, save_delay_s=MONTH_SAVE_DELAY_S
            )
            self._running_store = DayStore(
                hass, running_store_key(entry_id), self._running_dict, save_delay_s=RUNNING_SAVE_DELAY_S
            )
            self._days_store = DayStore(
                hass, days_store_key(entry_id), self.archive.as_dict, save_delay_s=MONTH_SAVE_DELAY_S
            )

    def now(self) -> datetime:
        """The site's clock (passed in for tests)."""
        return self._now()

    def _running_dict(self) -> dict[str, Any]:
        running = self.tracker.running
        return {"running": None if running is None else running.as_dict()}

    async def async_load(self) -> None:
        """Read what was kept before the restart; observations before this are dropped (nothing to add them to)."""
        if self._month_store is None or self._running_store is None:
            self._loaded = True
            return
        month = await self._month_store.async_load()
        running = await self._running_store.async_load()
        days = None if self._days_store is None else await self._days_store.async_load()
        try:
            self.tracker.restore(month, running.get("running") if isinstance(running, dict) else None)
        except Exception:  # noqa: BLE001 - what was kept is a measurement: a site never fails to start on it
            _LOGGER.debug("SpotNav site demand: the kept hours could not be read", exc_info=True)
        try:
            self.archive.restore(days)
        except Exception:  # noqa: BLE001 - as above
            _LOGGER.debug("SpotNav site demand: the kept days could not be read", exc_info=True)
        self._loaded = True

    async def async_close(self) -> None:
        for store in (self._month_store, self._running_store, self._days_store):
            if store is not None:
                await store.async_close()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The site is gone for good: forget its hours."""
        await DayStore.async_remove(hass, month_store_key(entry_id))
        await DayStore.async_remove(hass, running_store_key(entry_id))
        await DayStore.async_remove(hass, days_store_key(entry_id))

    def due(self) -> bool:
        """Whether a reading is wanted now: loaded, and `OBSERVE_MIN_S` since the last one."""
        if not self._loaded:
            return False
        if self._last_observed is None:
            return True
        elapsed = (self._now() - self._last_observed).total_seconds()
        return not 0 <= elapsed < OBSERVE_MIN_S

    def observe(
        self,
        grid_w: float | None,
        car_w: float | None,
        tz: str | None = None,
        discharge_w: float | None = None,
        *,
        target: TargetMark | None = None,
        components: Callable[[], Sequence[DemandComponent]] | None = None,
    ) -> None:
        """One reading of the site (at most every `OBSERVE_MIN_S`). `target` marks the hour the import target held or
        paused a charger in; `components` gives the demand charge's components when an hour completes (its month's
        days are filed against them). Never raises."""
        if not self.due():
            return
        try:
            now = self._now()
            self._last_observed = now
            self.readings += 1
            if tz is not None and tz != self.tracker.tz:
                self.tracker.tz = tz
            changed = self.tracker.observe(now, grid_w, car_w, discharge_w, target)
            if changed:
                if self._month_store is not None:
                    self._month_store.changed()
                self._file_days(now, () if components is None else components())
            running = self.tracker.running
            if self._running_store is not None and running is not None and (grid_w is not None or running.covered_s):
                # A site that measures nothing writes nothing.
                self._running_store.changed()
        except Exception:  # noqa: BLE001 - a measurement must never disturb the site
            _LOGGER.debug("SpotNav site demand: a reading could not be taken", exc_info=True)

    def _file_days(self, now: datetime, components: Sequence[DemandComponent]) -> None:
        """Refile the kept months' day summaries from their hours (an hour completed, or the month rolled)."""
        tracker = self.tracker
        for month, hours in ((tracker.previous_month, tracker.previous), (tracker.month, tracker.hours)):
            if month is not None:
                self.archive.file_month(month, hours, components, tracker.tz, now)
        if tracker.month is not None:
            self.archive.prune(tracker.month)
        if self._days_store is not None:
            self._days_store.changed()

    # ---- back-fill from Home Assistant's statistics

    def backfill_due(self) -> bool:
        """Whether the one back-fill is wanted: loaded, kept in stores, never run, and the site has measured."""
        if not self._loaded or self._days_store is None or self._backfill_running or self.archive.backfill is not None:
            return False
        running = self.tracker.running
        return self.tracker.month is not None and (bool(self.tracker.hours) or (running is not None and running.covered_s > 0))

    def backfill_wanted_again(self, now: datetime) -> bool:
        """Whether a back-fill that found nothing (or failed) may run again on the power view's request: at most once
        a day, never while one runs, never after one that filed hours."""
        marker = self.archive.backfill
        if not self._loaded or self._days_store is None or self._backfill_running or self.tracker.month is None:
            return False
        if marker is None:
            return True
        if marker.get("status") == BACKFILL_DONE and marker.get("hours"):
            return False
        at = _instant(marker.get("at"))
        return at is None or (now - at).total_seconds() >= BACKFILL_RETRY_S

    async def async_backfill(
        self, reader: Any, series: Any, components: Sequence[DemandComponent]
    ) -> str:
        """Read the statistics and file what SpotNav did not measure (see the module); the marker's status. Never
        raises."""
        from .hour_statistics import backfill_hours, read_site_hours, statistics_period  # noqa: PLC0415

        self._backfill_running = True
        now = self._now()
        status, filed = BACKFILL_FAILED, 0
        try:
            start, end = statistics_period(now)
            read = await read_site_hours(reader, series, start, end)
            if not read.grid_readable:
                status = BACKFILL_NO_STATISTICS
            else:
                filed = self.apply_backfill(backfill_hours(read.hours), components, now)
                status = BACKFILL_DONE
        except Exception:  # noqa: BLE001 - a back-fill must never disturb the site
            _LOGGER.debug("SpotNav site demand: the statistics could not be read", exc_info=True)
        finally:
            self._backfill_running = False
        self.archive.backfill = {"at": now.isoformat(), "status": status, "hours": filed}
        if self._days_store is not None:
            self._days_store.changed()
        _LOGGER.info("SpotNav site demand: back-fill from Home Assistant's statistics %s, %d hours", status, filed)
        return status

    def apply_backfill(self, imported: Sequence[HourMean], components: Sequence[DemandComponent], now: datetime) -> int:
        """File `imported` hours (see the module): only those before the first hour SpotNav measured, the running
        month's and the month before's as hours, older months' as day summaries where the archive keeps none. How
        many hours were filed."""
        tracker = self.tracker
        current = tracker.month
        if current is None:
            return 0
        measured = [hour.start for hour in (*tracker.previous, *tracker.hours) if not hour.imported]
        if tracker.running is not None:
            measured.append(tracker.running.start)
        if not measured:
            return 0
        cutoff = min(measured)
        kept = {hour.start for hour in (*tracker.previous, *tracker.hours)}
        oldest = months_before(current, 3)
        by_month: dict[tuple[int, int], list[HourMean]] = {}
        for hour in imported:
            if hour.start >= cutoff or hour.start in kept:
                continue
            month = month_of(hour.start, tracker.tz)
            if oldest <= month <= current:
                by_month.setdefault(month, []).append(hour)
        previous = months_before(current, 1)
        filed = 0
        for month, hours in by_month.items():
            if month == current:
                tracker.hours = sorted([*tracker.hours, *hours], key=lambda hour: hour.start)
            elif month == previous:
                if tracker.previous_month != previous:
                    tracker.previous_month, tracker.previous = previous, []
                tracker.previous = sorted([*tracker.previous, *hours], key=lambda hour: hour.start)
            elif month in self.archive.months:
                continue
            else:
                self.archive.file_month(month, hours, components, tracker.tz, now)
            filed += len(hours)
        if filed:
            if self._month_store is not None:
                self._month_store.changed()
            self._file_days(now, components)
        return filed

    def tracker_months(self) -> dict[tuple[int, int], Sequence[HourMean]]:
        """The months whose every hour the tracker keeps, and those hours."""
        tracker = self.tracker
        months: dict[tuple[int, int], Sequence[HourMean]] = {}
        if tracker.previous_month is not None:
            months[tracker.previous_month] = tracker.previous
        if tracker.month is not None:
            months[tracker.month] = tracker.hours
        return months

    def power_month(
        self,
        month: tuple[int, int],
        components: Sequence[DemandComponent],
        *,
        import_target_kw: float | None,
        tz: str | None = None,
    ) -> dict[str, Any]:
        """One month's power view (`site/power_history.py:power_month`), on `tz`'s clock (the site's area's; the
        tracker's own until it is known)."""
        return power_month(
            month,
            tracker_months=self.tracker_months(),
            archive=self.archive,
            components=components,
            tz=tz or self.tracker.tz,
            now=self._now(),
            import_target_kw=import_target_kw,
        )

    def snapshot(
        self,
        components: Sequence[DemandComponent],
        *,
        import_target_kw: float | None,
        now: datetime | None = None,
    ) -> DemandSnapshot:
        """What the month's hours bill per component, and the running hour, at `now`."""
        moment = self._now() if now is None else now
        tz = self.tracker.tz
        hours = self.tracker.month_hours(moment)
        running = self.tracker.running_hour(moment)
        return DemandSnapshot(
            tz=tz,
            month=self.tracker.month,
            components=tuple(component_month(component, hours, tz, moment) for component in components),
            hours_kept=len(hours),
            incomplete_hours=sum(1 for hour in hours if hour.incomplete),
            running=running,
            import_target_kw=import_target_kw,
        )


    def diagnostics(self, components: Sequence[DemandComponent], *, import_target_kw: float | None) -> dict[str, Any]:
        """The site's `demand` diagnostics: the components and what each bills so far, the running hour with its
        gaps, the import target, every hour of the month (`hour_row`), and how many months keep day summaries."""
        now = self._now()
        snapshot = self.snapshot(components, import_target_kw=import_target_kw, now=now)
        running = snapshot.running
        house = (
            None
            if running is None or running.house_covered_s <= 0
            else round(running.house_wh / running.house_covered_s * 3.6, 3)
        )
        mean = None if running is None else running.mean_kw()
        return {
            "tz": snapshot.tz,
            "month": _month_text(snapshot.month),
            "readings": self.readings,
            "import_target_kw": import_target_kw,
            "components": [
                {
                    **component.as_dict(),
                    "bar_kw": month.bar_kw,
                    "free_kw": month.free_kw,
                    "days_counted": month.days_counted,
                    "peaks": [
                        {"date": peak.day.isoformat(), "start": peak.start.isoformat(), "kw": round(peak.kw, 3)}
                        for peak in month.peaks
                    ],
                }
                for component, month in zip(components, snapshot.components, strict=True)
            ],
            "running": None
            if running is None
            else {
                "start": running.start.isoformat(),
                "kw_so_far": None if mean is None else round(mean, 3),
                "kwh_so_far": round(running.import_wh / 1000, 3),
                "covered_s": round(running.covered_s, 1),
                "gap_s": round(running.gap_s, 1),
                "house_kw_so_far": house,
            },
            "hours_kept": snapshot.hours_kept,
            "incomplete_hours": snapshot.incomplete_hours,
            "hours": [hour_row(hour) for hour in self.tracker.month_hours(now)],
            "months_with_days": len(self.archive.months),
            "backfill": self.archive.backfill,
        }


@dataclass(frozen=True, slots=True)
class DemandSnapshot:
    """`SiteDemand.snapshot`: per component what it bills so far, and the running hour."""

    tz: str
    month: tuple[int, int] | None
    components: tuple[Any, ...]
    hours_kept: int
    incomplete_hours: int
    running: RunningHour | None
    import_target_kw: float | None
