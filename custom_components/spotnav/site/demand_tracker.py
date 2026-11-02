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
* **An hour's mean** is its energy over the time it was measured. An hour with more than `GAP_MAX_S` of gaps is
  `incomplete`: kept and shown, but it never raises a peak. An hour never measured at all is not kept.
* **Restart mid-hour**: the running hour is kept (`RUNNING_SAVE_DELAY_S`, at most once a minute); after a restart
  the time since its last write is a gap, so a restart longer than `GAP_MAX_S` makes that hour incomplete.
* **Month**: the hours of the running month on the area's clock, and those of the month before (for the floor a
  later release uses). At the first hour of a new month the month's hours become the previous month's.

`HourTracker` is pure (the clock is passed in); `SiteDemand` keeps it in the site's own two stores (the month's
hours, written when an hour completes; the running hour, at most once a minute). Storage is untrusted: a malformed
record is dropped, never raised on.
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


def month_store_key(entry_id: str) -> str:
    return f"{DOMAIN}.demand.{entry_id}"


def running_store_key(entry_id: str) -> str:
    return f"{DOMAIN}.demand_hour.{entry_id}"


@dataclass(slots=True)
class RunningHour:
    """The clock hour being measured: its start (UTC), energy and measured time so far, and the last reading."""

    start: datetime
    import_wh: float = 0.0
    covered_s: float = 0.0
    house_wh: float = 0.0
    house_covered_s: float = 0.0
    #: When the last observation was made, and what it read (`None`: no usable reading, or a restart since).
    last_at: datetime | None = None
    last_grid_w: float | None = None
    last_car_w: float | None = None

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
            "last_at": None if self.last_at is None else self.last_at.isoformat(),
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
        return cls(
            start=start,
            import_wh=float(numbers[0]),
            covered_s=float(numbers[1]),
            house_wh=float(numbers[2]),
            house_covered_s=float(numbers[3]),
            last_at=last_at,
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


def _hour_row(hour: HourMean) -> list[Any]:
    return [
        hour.start.isoformat(),
        round(hour.kw, 3),
        None if hour.house_kw is None else round(hour.house_kw, 3),
        1 if hour.incomplete else 0,
    ]


def _hour_from_row(raw: Any) -> HourMean | None:
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    start = _instant(raw[0])
    kw, house, incomplete = raw[1], raw[2], raw[3]
    if start is None or not _number(kw) or kw < 0 or incomplete not in (0, 1):
        return None
    if house is not None and (not _number(house) or house < 0):
        return None
    return HourMean(start=start, kw=float(kw), house_kw=None if house is None else float(house), incomplete=bool(incomplete))


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

    def observe(self, now: datetime, grid_w: float | None, car_w: float | None) -> bool:
        """Take a reading at `now`: the time since the last one is integrated at the last reading (held at most
        `HOLD_MAX_S`), completed hours are kept, and from now on `grid_w`/`car_w` are held. Returns whether the
        month's hours changed (an hour completed, or the month rolled)."""
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
                )
        running.last_at = now
        running.last_grid_w = None if grid_w is None or not _number(grid_w) else float(grid_w)
        running.last_car_w = None if car_w is None or not _number(car_w) else float(car_w)
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
            "hours": [_hour_row(hour) for hour in self.hours],
            "previous_month": _month_text(self.previous_month),
            "previous": [_hour_row(hour) for hour in self.previous],
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
        self._now = now
        self._last_observed: datetime | None = None
        self._loaded = hass is None or entry_id is None
        self._month_store: DayStore | None = None
        self._running_store: DayStore | None = None
        if hass is not None and entry_id is not None:
            self._month_store = DayStore(
                hass, month_store_key(entry_id), self.tracker.month_dict, save_delay_s=MONTH_SAVE_DELAY_S
            )
            self._running_store = DayStore(
                hass, running_store_key(entry_id), self._running_dict, save_delay_s=RUNNING_SAVE_DELAY_S
            )

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
        try:
            self.tracker.restore(month, running.get("running") if isinstance(running, dict) else None)
        except Exception:  # noqa: BLE001 - what was kept is a measurement: a site never fails to start on it
            _LOGGER.debug("SpotNav site demand: the kept hours could not be read", exc_info=True)
        self._loaded = True

    async def async_close(self) -> None:
        for store in (self._month_store, self._running_store):
            if store is not None:
                await store.async_close()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The site is gone for good: forget its hours."""
        await DayStore.async_remove(hass, month_store_key(entry_id))
        await DayStore.async_remove(hass, running_store_key(entry_id))

    def observe(self, grid_w: float | None, car_w: float | None, tz: str | None = None) -> None:
        """One reading of the site (at most every `OBSERVE_MIN_S`). Never raises."""
        if not self._loaded:
            return
        try:
            now = self._now()
            if self._last_observed is not None and 0 <= (now - self._last_observed).total_seconds() < OBSERVE_MIN_S:
                return
            self._last_observed = now
            if tz is not None and tz != self.tracker.tz:
                self.tracker.tz = tz
            changed = self.tracker.observe(now, grid_w, car_w)
            if changed and self._month_store is not None:
                self._month_store.changed()
            if self._running_store is not None:
                self._running_store.changed()
        except Exception:  # noqa: BLE001 - a measurement must never disturb the site
            _LOGGER.debug("SpotNav site demand: a reading could not be taken", exc_info=True)

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
