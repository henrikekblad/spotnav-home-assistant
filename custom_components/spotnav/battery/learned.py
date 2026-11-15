"""A battery's size and rated power learnt from what the plan already measures (pure).

Some batteries do not say how big they are (GoodWe's core integration has no size; SolaX and Enphase have no rated
power). There is no setting for it (fewest settings): the quarter hours the battery plan records (`PlanActuals`:
the battery's energy and the SoC at the start and end of each) show it.

* **Capacity.** A *steady run* is consecutive, fully measured quarter hours in which the battery moved the same way
  at least `STEADY_KW` and the SoC never jumped or turned against it. A run whose SoC moved `MIN_SOC_SPAN` points or
  more gives one sample: the energy that went into (or out of) the cells, `energy x sqrt(round trip)` charging or
  `/ sqrt(round trip)` discharging, over the SoC change. The capacity is the median of `MIN_SAMPLES` or more samples
  (the last `MAX_SAMPLES` kept, one per run's end), and only when at least `MIN_SAMPLES` of them lie within
  `AGREE_SHARE` of that median: runs that disagree (a SoC that is not linear, a meter that lies) teach nothing.
* **Rated power.** The highest power two consecutive fully measured quarter hours both held, charging and
  discharging apart. It is a floor: the battery may well manage more than it was ever asked for, and the
  plant's own limit shows in it. It only ever rises.

Both are estimates: SpotNav marks them (`capacity_learned`, `rated_learned`) and never lets one replace a value the
integration reads. What this cannot tell: a battery that was never cycled 20 points, or never charged at its limit,
stays unknown (the plan then says why); the SoC's own scaling (a battery that shows 10-90 % of its cells as 0-100 %)
is the size of the part the SoC covers, which is what the plan wants.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from .facts import BatteryFacts
from .plan_model import DEFAULT_ROUND_TRIP_EFFICIENCY

if TYPE_CHECKING:
    from .plan_shadow import Bucket

EST_CAPACITY_LEARNED: Final = "capacity_learned"
EST_RATED_LEARNED: Final = "rated_learned"

BUCKET: Final = timedelta(minutes=15)
#: A quarter hour counts only when this much of it was measured (s).
FULL_COVER_S: Final = 0.9 * BUCKET.total_seconds()
#: The least mean power (kW) of a quarter hour in a steady run.
STEADY_KW: Final = 0.3
#: The SoC (points) between one quarter hour's end and the next one's start that is no jump, and the most a quarter
#: hour's SoC may move against the run's direction (a reading's rounding).
MAX_SOC_JUMP: Final = 2.0
#: A run teaches only when its SoC moved this many points.
MIN_SOC_SPAN: Final = 20.0
#: What a believable capacity and a believable rating are (kWh, kW).
CAPACITY_RANGE: Final = (0.5, 250.0)
MAX_RATED_KW: Final = 100.0
MIN_SAMPLES: Final = 3
MAX_SAMPLES: Final = 15
#: Samples within this share of the median agree with it.
AGREE_SHARE: Final = 0.25


@dataclass(slots=True)
class LearnedBattery:
    """What was learnt of one site's battery; persisted (`as_dict`/`from_dict`)."""

    #: `(end of the run's last quarter hour, kWh)`, oldest first.
    samples: list[tuple[datetime, float]] = field(default_factory=list)
    rated_charge_kw: float | None = None
    rated_discharge_kw: float | None = None

    # ---- the answers

    @property
    def capacity_kwh(self) -> float | None:
        """The median of the samples when enough of them agree, else `None`."""
        if len(self.samples) < MIN_SAMPLES:
            return None
        values = [kwh for _end, kwh in self.samples]
        median = statistics.median(values)
        agreeing = [kwh for kwh in values if abs(kwh - median) <= AGREE_SHARE * median]
        if len(agreeing) < MIN_SAMPLES:
            return None
        return round(statistics.median(agreeing), 2)

    # ---- learning

    def learn(
        self, buckets: Iterable[Bucket], now: datetime, round_trip: float = DEFAULT_ROUND_TRIP_EFFICIENCY
    ) -> bool:
        """Take in the quarter hours kept; `True` when something new was learnt. Idempotent over the same buckets."""
        ordered = sorted((b for b in buckets if b.end <= now), key=lambda b: b.start)
        changed = False
        for run in _runs(ordered, now):
            changed |= self._take_run(run, round_trip)
        changed |= self._take_rated(ordered)
        return changed

    def _take_run(self, run: list[Bucket], round_trip: float) -> bool:
        end = run[-1].end
        if any(known == end for known, _kwh in self.samples):
            return False
        first, last = run[0].soc_start, run[-1].soc_end
        if first is None or last is None:
            return False
        span = last - first
        if abs(span) < MIN_SOC_SPAN:
            return False
        energy = sum(b.battery_kwh for b in run)
        if energy * span <= 0:
            return False
        root = math.sqrt(min(1.0, max(0.5, round_trip)))
        stored = abs(energy) * root if energy > 0 else abs(energy) / root
        kwh = stored / (abs(span) / 100.0)
        if not CAPACITY_RANGE[0] <= kwh <= CAPACITY_RANGE[1]:
            return False
        self.samples = sorted([*self.samples, (end, kwh)], key=lambda item: item[0])[-MAX_SAMPLES:]
        return True

    def _take_rated(self, ordered: list[Bucket]) -> bool:
        changed = False
        previous: float | None = None
        previous_end: datetime | None = None
        for bucket in ordered:
            kw = _mean_kw(bucket)
            if kw is None or previous is None or previous_end != bucket.start or previous * kw <= 0:
                previous, previous_end = kw, bucket.end
                continue
            held = round(min(abs(previous), abs(kw)), 1)
            previous, previous_end = kw, bucket.end
            if held > MAX_RATED_KW:
                continue
            if kw > 0 and held > (self.rated_charge_kw or 0.0):
                self.rated_charge_kw, changed = held, True
            elif kw < 0 and held > (self.rated_discharge_kw or 0.0):
                self.rated_discharge_kw, changed = held, True
        return changed

    # ---- persistence and diagnostics

    def as_dict(self) -> dict[str, Any]:
        return {
            "samples": [[end.isoformat(), round(kwh, 3)] for end, kwh in self.samples],
            "rated_charge_kw": self.rated_charge_kw,
            "rated_discharge_kw": self.rated_discharge_kw,
        }

    def view(self) -> dict[str, Any]:
        """For the diagnostics: the answers and what they rest on."""
        return {
            "capacity_kwh": self.capacity_kwh,
            "capacity_samples": [round(kwh, 2) for _end, kwh in self.samples],
            "rated_charge_kw": self.rated_charge_kw,
            "rated_discharge_kw": self.rated_discharge_kw,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> LearnedBattery:
        if not isinstance(raw, dict):
            return cls()
        samples: list[tuple[datetime, float]] = []
        stored = raw.get("samples")
        for item in stored if isinstance(stored, list) else []:
            try:
                end = datetime.fromisoformat(str(item[0]))
                kwh = float(item[1])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if end.tzinfo is not None and math.isfinite(kwh) and CAPACITY_RANGE[0] <= kwh <= CAPACITY_RANGE[1]:
                samples.append((end, kwh))
        return cls(
            samples=sorted(samples, key=lambda item: item[0])[-MAX_SAMPLES:],
            rated_charge_kw=_rating(raw.get("rated_charge_kw")),
            rated_discharge_kw=_rating(raw.get("rated_discharge_kw")),
        )


def _rating(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value) if 0 < value <= MAX_RATED_KW else None


def _mean_kw(bucket: Bucket) -> float | None:
    """The battery's mean power over a fully measured quarter hour (kW, + charging), else `None`."""
    if bucket.covered_s < FULL_COVER_S:
        return None
    return bucket.battery_kwh / (bucket.covered_s / 3600.0)


def _runs(ordered: list[Bucket], now: datetime) -> list[list[Bucket]]:
    """The steady runs that are over: no quarter hour of one follows another, or a later quarter hour has passed
    (a run still going is left for later, so it is never counted twice at two lengths)."""
    runs: list[list[Bucket]] = []
    run: list[Bucket] = []

    def close() -> None:
        nonlocal run
        if run:
            runs.append(run)
        run = []

    for bucket in ordered:
        kw = _mean_kw(bucket)
        steady = kw is not None and abs(kw) >= STEADY_KW and bucket.soc_start is not None and bucket.soc_end is not None
        if steady and run:
            previous = run[-1]
            sign = _mean_kw(previous) or 0.0
            direction = 1.0 if sign > 0 else -1.0
            moved = (bucket.soc_end - bucket.soc_start) * direction  # type: ignore[operator]
            joined = (
                previous.end == bucket.start
                and sign * (kw or 0.0) > 0
                and abs(bucket.soc_start - previous.soc_end) <= MAX_SOC_JUMP  # type: ignore[operator]
                and moved >= -MAX_SOC_JUMP
            )
            if not joined:
                close()
        if steady:
            moved = ((bucket.soc_end or 0.0) - (bucket.soc_start or 0.0)) * (1.0 if (kw or 0.0) > 0 else -1.0)
            if moved < -MAX_SOC_JUMP:
                close()
                continue
            run.append(bucket)
        else:
            close()
    if run and run[-1].end <= now - BUCKET:
        close()
    else:
        run = []
    return runs


def with_learned(facts: BatteryFacts, learned: LearnedBattery) -> BatteryFacts:
    """The facts with what was learnt filled in where the integration reads nothing (never over a read value); the
    estimates say which."""
    updates: dict[str, Any] = {}
    estimates = list(facts.estimates)
    sources = dict(facts.sources)
    if facts.capacity_kwh is None and learned.capacity_kwh is not None:
        updates["capacity_kwh"] = learned.capacity_kwh
        estimates.append(EST_CAPACITY_LEARNED)
        sources["capacity"] = "learned"
    rated = False
    if facts.rated_charge_kw is None and learned.rated_charge_kw is not None:
        updates["rated_charge_kw"] = learned.rated_charge_kw
        sources["rated_charge"] = "learned"
        rated = True
    if facts.rated_discharge_kw is None and learned.rated_discharge_kw is not None:
        updates["rated_discharge_kw"] = learned.rated_discharge_kw
        sources["rated_discharge"] = "learned"
        rated = True
    if rated:
        estimates.append(EST_RATED_LEARNED)
    if not updates:
        return facts
    return replace(facts, estimates=tuple(estimates), sources=sources, **updates)
