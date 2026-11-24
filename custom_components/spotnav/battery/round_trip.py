"""A home battery's round trip learnt from its own meters (pure).

The plan prices every stored kWh with the battery's round trip: what comes out over what went in. It used to be
assumed to be 90 %; some batteries do clearly worse. There is no setting for it (fewest settings): the battery's own
meters show it.

* **A window** runs from one moment to a later one at which the battery's level is the same again (within
  `SOC_TOLERANCE` points) and at least `MIN_CHARGED_KWH` went into it in between. What went in and what came out in it
  are then the same energy stored once, so out over in is the round trip, standby use included. The level is looked at
  every quarter hour; when it passed the start's level inside a quarter hour, the end is put where it passed it, with
  the energies in proportion. A window ends at the first such moment and the next one starts there. One that is not
  over after `MAX_WINDOW` starts again.
* **What went in and out** comes from the battery's lifetime counters (`adapters`: `charged_total`,
  `discharged_total`) where the brand has them, else from the battery's power that SpotNav measures each quarter hour
  (`plan_shadow.Bucket.battery_kwh`, + charging). Counters are preferred: they count every second. A counter that falls
  or climbs faster than the battery could charge (`_jumped`: a reset, a wrap, a replaced battery) spoils the windows
  it falls in; a quarter hour that was not fully measured, or missing, spoils them for the measured power.
* **A guard.** Where both are known, they must agree within `AGREE_SHARE` on what went in and on what came out. A
  counter that counts something else (the panels' energy on one side of a hybrid and not on the other, the grid's
  share only) teaches nothing: the window is thrown away (`counters_disagree`).
* **The answer.** A window's round trip must be believable (`PLAUSIBLE`). The round trip is the median of the last
  `RECENT` windows' that lie within `AGREE_POINTS` of their median, once `MIN_WINDOWS` of them do. The plan's value
  (`efficiency`) moves slowly: it follows the median only when that is `DEADBAND` or more away, and then at most
  `MAX_STEP` per window, so a plan never flaps on one odd window.
* **A cap.** The plan's value is never above `PLAN_CAP`, though a window up to `PLAUSIBLE`'s top is taken. Most
  brands' counters, and the battery power SpotNav measures, count at the battery (DC, `adapters`): the inverter's own
  loss on the way in and again on the way out (about 2-3 % each) is not in them, so a DC round trip of 97 % is an AC
  one of 91-93 %. The plan buys and sells on the AC side, and no home battery does better than about 95 % there, so a
  higher value would make it cycle for spreads that do not pay. The raw median stays in the diagnostics (`view`).
  Where the battery's power is marked as read at the battery (`adapters`: `dc_power`), the inverter's loss is known
  (`conversion_loss.py`) and the plan takes the DC round trip times it instead (`learned.with_learned`): the value
  here is then capped only at `DC_CAP` (`cap`, set by the shell), so the AC round trip is not cut twice.

Every window taken is kept with its date (the last `KEEP` days), for the diagnostics and a chart later.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .plan_shadow import Bucket

BUCKET: Final = timedelta(minutes=15)
FULL_COVER_S: Final = 0.9 * BUCKET.total_seconds()
#: The level (points) at a window's end that counts as the start's.
SOC_TOLERANCE: Final = 1.5
#: The least that must go into the battery in a window (kWh).
MIN_CHARGED_KWH: Final = 20.0
#: The longest a window may run.
MAX_WINDOW: Final = timedelta(days=14)
#: The highest step between two quarter hours' points over which the level is put in between them.
MAX_STEP_GAP: Final = timedelta(minutes=20)
#: A believable round trip (a window's, counted at the battery's terminals).
PLAUSIBLE: Final = (0.60, 0.98)
#: The highest round trip the plan uses (see the module: counters at the battery miss the inverter's loss).
PLAN_CAP: Final = 0.95
#: The cap where the battery's power is read at the battery and the plan turns the value into an AC one itself.
DC_CAP: Final = PLAUSIBLE[1]
#: Counters and measured power must agree within this share.
AGREE_SHARE: Final = 0.15
#: The windows looked at for the answer, and those within this many hundredths of their median agree.
RECENT: Final = 9
AGREE_POINTS: Final = 0.05
MIN_WINDOWS: Final = 3
#: The plan's value follows the median only past the deadband, and at most this much per window.
DEADBAND: Final = 0.02
MAX_STEP: Final = 0.03
#: The windows kept, for the diagnostics and a chart.
KEEP: Final = timedelta(days=730)
#: The rejected windows kept for the diagnostics.
KEEP_REJECTED: Final = 10
#: The fastest a counter may climb when the battery's rating is not known (kW), and the slack on any rating.
FALLBACK_MAX_KW: Final = 100.0
COUNTER_SLACK: Final = 1.5
#: A counter's fall that is only a reading's rounding (kWh).
COUNTER_ROUNDING: Final = 0.05

SOURCE_COUNTERS: Final = "counters"
SOURCE_METERS: Final = "meters"
REJECT_DISAGREE: Final = "counters_disagree"
REJECT_IMPLAUSIBLE: Final = "implausible"


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


@dataclass(frozen=True, slots=True)
class Point:
    """A moment: the level, the counters (kWh) and the measured energy in and out since learning began (kWh)."""

    at: datetime
    soc: float
    charged: float | None
    discharged: float | None
    meter_in: float
    meter_out: float

    def between(self, later: Point, share: float) -> Point:
        """The point `share` of the way to `later`, everything in proportion."""

        def mix(a: float | None, b: float | None) -> float | None:
            return None if a is None or b is None else a + (b - a) * share

        return Point(
            at=self.at + (later.at - self.at) * share,
            soc=self.soc + (later.soc - self.soc) * share,
            charged=mix(self.charged, later.charged),
            discharged=mix(self.discharged, later.discharged),
            meter_in=self.meter_in + (later.meter_in - self.meter_in) * share,
            meter_out=self.meter_out + (later.meter_out - self.meter_out) * share,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "soc": round(self.soc, 2),
            "charged": None if self.charged is None else round(self.charged, 4),
            "discharged": None if self.discharged is None else round(self.discharged, 4),
            "meter_in": round(self.meter_in, 4),
            "meter_out": round(self.meter_out, 4),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Point | None:
        if not isinstance(raw, dict):
            return None
        try:
            at = datetime.fromisoformat(str(raw["at"]))
            soc, meter_in, meter_out = float(raw["soc"]), float(raw["meter_in"]), float(raw["meter_out"])
        except (KeyError, TypeError, ValueError):
            return None
        charged, discharged = raw.get("charged"), raw.get("discharged")
        if at.tzinfo is None or not all(math.isfinite(v) for v in (soc, meter_in, meter_out)):
            return None
        return cls(
            at, soc,
            float(charged) if _finite(charged) else None,
            float(discharged) if _finite(discharged) else None,
            meter_in, meter_out,
        )


@dataclass(frozen=True, slots=True)
class Window:
    """One window's result: what went in and came out (kWh), out over in, and what it was counted with."""

    start: datetime
    end: datetime
    charged_kwh: float
    discharged_kwh: float
    efficiency: float
    source: str
    #: Why it was thrown away, `None` for one taken.
    rejected: str | None = None

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "charged_kwh": round(self.charged_kwh, 2),
            "discharged_kwh": round(self.discharged_kwh, 2),
            "efficiency": round(self.efficiency, 4),
            "source": self.source,
        }
        if self.rejected is not None:
            row["rejected"] = self.rejected
        return row

    @classmethod
    def from_dict(cls, raw: Any) -> Window | None:
        if not isinstance(raw, dict):
            return None
        try:
            start = datetime.fromisoformat(str(raw["start"]))
            end = datetime.fromisoformat(str(raw["end"]))
            charged, discharged = float(raw["charged_kwh"]), float(raw["discharged_kwh"])
            efficiency = float(raw["efficiency"])
        except (KeyError, TypeError, ValueError):
            return None
        source, rejected = raw.get("source"), raw.get("rejected")
        if start.tzinfo is None or end.tzinfo is None or source not in (SOURCE_COUNTERS, SOURCE_METERS):
            return None
        if not all(math.isfinite(v) for v in (charged, discharged, efficiency)):
            return None
        return cls(start, end, charged, discharged, efficiency, source, rejected if isinstance(rejected, str) else None)


@dataclass(slots=True)
class RoundTrip:
    """What was learnt of one battery's round trip; persisted (`as_dict`/`from_dict`)."""

    #: The windows taken, oldest first (the last `KEEP`), and the last ones thrown away.
    windows: list[Window] = field(default_factory=list)
    rejected: list[Window] = field(default_factory=list)
    #: The round trip the plan uses, `None` until enough windows agree.
    efficiency: float | None = None
    #: The open window's start and the last point (for a level passed inside a quarter hour).
    anchor: Point | None = None
    previous: Point | None = None
    #: The measured energy in and out since learning began (kWh), counted up to `meter_through`.
    meter_in: float = 0.0
    meter_out: float = 0.0
    meter_through: datetime | None = None
    #: The last moment the measured power or a counter could not be trusted up to: a window that starts before it
    #: cannot use it.
    meter_break: datetime | None = None
    counter_break: datetime | None = None
    #: The last counters read: `(when, in, out)`.
    counters_last: tuple[datetime, float, float] | None = None
    #: The highest value the plan's round trip takes (`PLAN_CAP`, or `DC_CAP`: see the module).
    cap: float = PLAN_CAP

    # ---- the answers

    @property
    def median(self) -> float | None:
        """The median of the recent windows that agree, once `MIN_WINDOWS` do; else `None`."""
        recent = [window.efficiency for window in self.windows[-RECENT:]]
        if len(recent) < MIN_WINDOWS:
            return None
        middle = statistics.median(recent)
        agreeing = [value for value in recent if abs(value - middle) <= AGREE_POINTS]
        if len(agreeing) < MIN_WINDOWS:
            return None
        return statistics.median(agreeing)

    def set_cap(self, cap: float) -> None:
        """Take [cap] as the highest value (see the module); a value held at the old cap moves to the new one now."""
        if cap == self.cap:
            return
        self.cap = cap
        median = self.median
        if median is not None:
            self.efficiency = round(min(cap, max(PLAUSIBLE[0], median)), 3)
        elif self.efficiency is not None:
            self.efficiency = min(cap, self.efficiency)

    # ---- learning

    def observe(
        self,
        at: datetime,
        soc: float | None,
        counters: tuple[float | None, float | None],
        buckets: Iterable[Bucket],
        max_kw: float | None = None,
    ) -> bool:
        """One look at a quarter hour's turn: the level and the counters now, and the quarter hours kept (those that
        ended since the last look are counted). `True` when a window was taken or thrown away."""
        self._count_meters(at, buckets)
        charged, discharged = counters
        if not (_finite(charged) and _finite(discharged)):
            charged = discharged = None
        else:
            self._check_counters(at, float(charged), float(discharged), max_kw)  # type: ignore[arg-type]
        if soc is None or not _finite(soc):
            self.previous = None
            return False
        point = Point(at, float(soc), charged, discharged, self.meter_in, self.meter_out)
        previous, self.previous = self.previous, point
        anchor = self.anchor
        if anchor is None or at - anchor.at > MAX_WINDOW or at <= anchor.at:
            self.anchor = point
            return False
        end = self._end(anchor, previous, point)
        if end is None:
            if not self._usable(anchor, point):
                self.anchor = point
            return False
        self.anchor = end
        return self._close(anchor, end)

    def _count_meters(self, at: datetime, buckets: Iterable[Bucket]) -> None:
        quarter = _quarter(at)
        if self.meter_through is None:
            self.meter_through = quarter
            return
        for bucket in sorted((b for b in buckets if self.meter_through <= b.start and b.end <= quarter), key=lambda b: b.start):
            if bucket.start != self.meter_through or bucket.covered_s < FULL_COVER_S:
                self.meter_break = bucket.end
            if bucket.battery_kwh >= 0:
                self.meter_in += bucket.battery_kwh
            else:
                self.meter_out -= bucket.battery_kwh
            self.meter_through = bucket.end
        if self.meter_through != quarter:
            # Quarter hours not kept (a restart): what was measured in them is not known.
            self.meter_break, self.meter_through = quarter, max(self.meter_through, quarter)

    def _check_counters(self, at: datetime, charged: float, discharged: float, max_kw: float | None) -> None:
        last = self.counters_last
        self.counters_last = (at, charged, discharged)
        if last is None:
            return
        hours = max(0.0, (at - last[0]).total_seconds() / 3600.0)
        limit = (max_kw if max_kw and max_kw > 0 else FALLBACK_MAX_KW) * COUNTER_SLACK * hours + 1.0
        if _jumped(charged - last[1], limit) or _jumped(discharged - last[2], limit):
            self.counter_break = at

    def _usable(self, anchor: Point, point: Point) -> bool:
        return self._counted(anchor, point) is not None or self._metered(anchor)

    def _counted(self, start: Point, end: Point) -> tuple[float, float] | None:
        if start.charged is None or start.discharged is None or end.charged is None or end.discharged is None:
            return None
        if self.counter_break is not None and self.counter_break > start.at:
            return None
        return end.charged - start.charged, end.discharged - start.discharged

    def _metered(self, start: Point) -> bool:
        """Whether every quarter hour since `start` was fully measured."""
        return self.meter_break is None or self.meter_break <= start.at

    def _charged(self, start: Point, end: Point) -> float | None:
        counted = self._counted(start, end)
        if counted is not None:
            return counted[0]
        if self._metered(start):
            return end.meter_in - start.meter_in
        return None

    def _end(self, anchor: Point, previous: Point | None, point: Point) -> Point | None:
        """Where the open window ends: at `point` when its level is the start's, else where the level passed the
        start's since `previous`; `None` when it does not end here."""
        candidates: list[Point] = []
        if previous is not None and previous.at > anchor.at and point.at - previous.at <= MAX_STEP_GAP:
            before, after = previous.soc - anchor.soc, point.soc - anchor.soc
            if before * after < 0 and abs(after) > SOC_TOLERANCE:
                candidates.append(previous.between(point, before / (before - after)))
        if abs(point.soc - anchor.soc) <= SOC_TOLERANCE:
            candidates.append(point)
        for end in candidates:
            charged = self._charged(anchor, end)
            if charged is not None and charged >= MIN_CHARGED_KWH:
                return end
        return None

    def _close(self, start: Point, end: Point) -> bool:
        counted = self._counted(start, end)
        metered = None if not self._metered(start) else (end.meter_in - start.meter_in, end.meter_out - start.meter_out)
        if counted is not None:
            charged, discharged = counted
            source = SOURCE_COUNTERS
        elif metered is not None:
            charged, discharged = metered
            source = SOURCE_METERS
        else:
            return False
        efficiency = discharged / charged if charged > 0 else 0.0
        reason: str | None = None
        if counted is not None and metered is not None and (
            _apart(counted[0], metered[0]) or _apart(counted[1], metered[1])
        ):
            reason = REJECT_DISAGREE
        elif not PLAUSIBLE[0] <= efficiency <= PLAUSIBLE[1]:
            reason = REJECT_IMPLAUSIBLE
        window = Window(start.at, end.at, charged, discharged, efficiency, source, reason)
        if reason is not None:
            self.rejected = [*self.rejected, window][-KEEP_REJECTED:]
            return True
        self.windows = [w for w in self.windows if w.end >= end.at - KEEP] + [window]
        self._settle()
        return True

    def _settle(self) -> None:
        median = self.median
        if median is None:
            return
        target = min(self.cap, max(PLAUSIBLE[0], median))
        if self.efficiency is None:
            self.efficiency = round(target, 3)
        elif abs(target - self.efficiency) >= DEADBAND:
            step = max(-MAX_STEP, min(MAX_STEP, target - self.efficiency))
            self.efficiency = round(self.efficiency + step, 3)

    # ---- persistence and diagnostics

    def as_dict(self) -> dict[str, Any]:
        return {
            "windows": [window.as_dict() for window in self.windows],
            "rejected": [window.as_dict() for window in self.rejected],
            "efficiency": self.efficiency,
            "anchor": None if self.anchor is None else self.anchor.as_dict(),
            "previous": None if self.previous is None else self.previous.as_dict(),
            "meter_in": round(self.meter_in, 4),
            "meter_out": round(self.meter_out, 4),
            "meter_through": None if self.meter_through is None else self.meter_through.isoformat(),
            "meter_break": None if self.meter_break is None else self.meter_break.isoformat(),
            "counter_break": None if self.counter_break is None else self.counter_break.isoformat(),
            "counters_last": None
            if self.counters_last is None
            else [self.counters_last[0].isoformat(), self.counters_last[1], self.counters_last[2]],
            "cap": self.cap,
        }

    def view(self) -> dict[str, Any]:
        """For the diagnostics: the answer, what it rests on and the windows kept (with their dates)."""
        median = self.median
        return {
            "efficiency": self.efficiency,
            # The raw median of what was measured, before the plan's cap (`PLAN_CAP`).
            "median": None if median is None else round(median, 4),
            "capped": median is not None and median > self.cap,
            "cap": self.cap,
            "windows": [window.as_dict() for window in self.windows],
            "rejected": [window.as_dict() for window in self.rejected],
            "open_since": None if self.anchor is None else self.anchor.at.isoformat(),
        }

    def series(self) -> list[dict[str, Any]]:
        """Each window taken as a dated value, oldest first, for a chart."""
        return [
            {"date": window.end.isoformat(), "efficiency": round(window.efficiency, 4), "source": window.source}
            for window in self.windows
        ]

    @classmethod
    def from_dict(cls, raw: Any) -> RoundTrip:
        if not isinstance(raw, dict):
            return cls()

        def windows(key: str) -> list[Window]:
            rows = raw.get(key)
            found = [Window.from_dict(row) for row in rows] if isinstance(rows, list) else []
            return sorted((w for w in found if w is not None), key=lambda w: w.end)

        efficiency = raw.get("efficiency")
        last = raw.get("counters_last")
        counters_last: tuple[datetime, float, float] | None = None
        if isinstance(last, list) and len(last) == 3 and _finite(last[1]) and _finite(last[2]):
            moment = _moment(last[0])
            counters_last = None if moment is None else (moment, float(last[1]), float(last[2]))
        meter_in, meter_out = raw.get("meter_in"), raw.get("meter_out")
        cap = DC_CAP if raw.get("cap") == DC_CAP else PLAN_CAP
        return cls(
            windows=[w for w in windows("windows") if w.rejected is None],
            rejected=windows("rejected")[-KEEP_REJECTED:],
            # A value stored before the cap (`PLAN_CAP`) is brought under it.
            efficiency=min(cap, float(efficiency))
            if _finite(efficiency) and PLAUSIBLE[0] <= efficiency <= PLAUSIBLE[1]
            else None,
            anchor=Point.from_dict(raw.get("anchor")),
            previous=Point.from_dict(raw.get("previous")),
            meter_in=float(meter_in) if _finite(meter_in) else 0.0,
            meter_out=float(meter_out) if _finite(meter_out) else 0.0,
            meter_through=_moment(raw.get("meter_through")),
            meter_break=_moment(raw.get("meter_break")),
            counter_break=_moment(raw.get("counter_break")),
            counters_last=counters_last,
            cap=cap,
        )


def _moment(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _quarter(at: datetime) -> datetime:
    return at.replace(minute=at.minute - at.minute % 15, second=0, microsecond=0)


def _jumped(delta: float, limit: float) -> bool:
    """A counter that fell (more than a reading's rounding) or climbed faster than the battery could."""
    return delta < -COUNTER_ROUNDING or delta > limit


def _apart(a: float, b: float) -> bool:
    return abs(a - b) > AGREE_SHARE * max(abs(a), abs(b), 1e-9)
