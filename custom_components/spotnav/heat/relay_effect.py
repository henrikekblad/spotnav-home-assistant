"""What a lowered block does to the heat pump's draw (plan `plans/v3_heating_plan.md` step 2 and section 7, stage 1):
less heat while the relay is closed, then a rebound after it, learned from the heat pump's meter.

Pure. Whoever closes the relay (the SpotNav blueprint, another automation, a person), a closed relay is a lowered
block, and the heat forecast must know it caused the dip and the rebound after it, so the cars' and the battery's
plans see the room it makes and the load that comes back.

* **A block's measurement** (`measure_block`): from the meter's 5-minute means with the hot-water runs left out
  (a run is the tank, not the house), the space heating's mean in the `BEFORE` before the block (`baseline_kw`), in
  the block (`during_kw`) and in the `AFTER` after it (`after_kw`). Saved: `(baseline - during) x the block's
  hours`; the rebound: `(after - baseline) x AFTER`. A block is measured only once its `AFTER` has passed, with at
  least `MIN_COVER` of each window's periods, no other block starting within its `AFTER` (the rebound would be the
  next block's dip) and at least `MIN_BLOCK` long. The outdoor temperature moving over the windows is the main error;
  a block whose outdoor mean moved more than `MAX_OUTDOOR_MOVE_C` between before and after is not counted.
* **What is learned** (`learn`): the share of the heat a block takes away (`lower_share`, saved over the baseline's
  energy in the block) and the share of that which comes back after (`rebound`, the rebound over the saved), each
  the mean of the measured blocks pulled towards a cautious prior by `PRIOR_WEIGHT` blocks' worth: half the heat
  taken away, all of it coming back (heat moved, not saved) until the meter shows otherwise. Clamped to
  `SHARE_RANGE` and `REBOUND_RANGE`. The learned figures only change what the forecast and the review count, never
  a guard.
* **The forecast's change** (`forecast_delta`): per hour from the running one on, less `lower_share` x the heat
  forecast for the share of the hour a block covers, and the block's whole rebound (`rebound` x what it saves, its
  past part included) spread evenly over the `AFTER` after it. A block's hours are the relay's closed hours, past
  (the rebound still to come), now and expected (`heat/relay_schedule.py`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

BEFORE: Final = timedelta(hours=2)
AFTER: Final = timedelta(hours=3)
MIN_BLOCK: Final = timedelta(minutes=30)
MIN_COVER: Final = 0.6
MIN_BASELINE_KW: Final = 0.2
MIN_SAVED_KWH: Final = 0.1
MAX_OUTDOOR_MOVE_C: Final = 3.0
PERIOD: Final = timedelta(minutes=5)
HOUR: Final = timedelta(hours=1)

PRIOR_SHARE: Final = 0.5
PRIOR_REBOUND: Final = 1.0
PRIOR_WEIGHT: Final = 5.0
SHARE_RANGE: Final = (0.0, 1.0)
REBOUND_RANGE: Final = (0.0, 2.0)
#: Measured blocks kept (newest last): about two months of a blueprint's daily blocks.
KEEP_BLOCKS: Final = 120


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    return min(bounds[1], max(bounds[0], value))


def _hours(span: timedelta) -> float:
    return span.total_seconds() / 3600.0


@dataclass(frozen=True, slots=True)
class BlockResult:
    """One measured block (kW means and kWh, see the module)."""

    start: datetime
    end: datetime
    baseline_kw: float
    during_kw: float
    after_kw: float
    outdoor_c: float | None = None

    @property
    def saved_kwh(self) -> float:
        return (self.baseline_kw - self.during_kw) * _hours(self.end - self.start)

    @property
    def rebound_kwh(self) -> float:
        return (self.after_kw - self.baseline_kw) * _hours(AFTER)

    def share(self) -> float | None:
        """The share of the block's heat it took away; `None` when the baseline is too small to tell."""
        if self.baseline_kw < MIN_BASELINE_KW:
            return None
        return _clamp(self.saved_kwh / (self.baseline_kw * _hours(self.end - self.start)), SHARE_RANGE)

    def rebound_share(self) -> float | None:
        """The share of the saved heat that came back after; `None` when too little was saved to tell."""
        if self.saved_kwh < MIN_SAVED_KWH:
            return None
        return _clamp(self.rebound_kwh / self.saved_kwh, REBOUND_RANGE)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "baseline_kw": round(self.baseline_kw, 3),
            "during_kw": round(self.during_kw, 3),
            "after_kw": round(self.after_kw, 3),
            "outdoor_c": None if self.outdoor_c is None else round(self.outdoor_c, 1),
            "saved_kwh": round(self.saved_kwh, 3),
            "rebound_kwh": round(self.rebound_kwh, 3),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> BlockResult | None:
        """A kept row, `None` for one that cannot be read."""
        if not isinstance(raw, Mapping):
            return None
        try:
            start = datetime.fromisoformat(str(raw["start"]))
            end = datetime.fromisoformat(str(raw["end"]))
            outdoor = raw.get("outdoor_c")
            return cls(
                start,
                end,
                float(raw["baseline_kw"]),
                float(raw["during_kw"]),
                float(raw["after_kw"]),
                None if outdoor is None else float(outdoor),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class Effect:
    """What a lowered block is counted to do: the share of the heat taken away, the share of that coming back, and
    how many measured blocks it is learned from (0: the prior alone)."""

    lower_share: float = PRIOR_SHARE
    rebound: float = PRIOR_REBOUND
    blocks: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"lower_share": round(self.lower_share, 3), "rebound": round(self.rebound, 3), "blocks": self.blocks}


def closed_intervals(states: Sequence[tuple[datetime, bool]], end: datetime) -> list[tuple[datetime, datetime]]:
    """The intervals the relay was closed, from its states in time order (each `(since, closed)`), the last one
    running to `end`."""
    out: list[tuple[datetime, datetime]] = []
    since: datetime | None = None
    for moment, closed in sorted(states, key=lambda item: item[0]):
        if closed and since is None:
            since = moment
        elif not closed and since is not None:
            if moment > since:
                out.append((since, moment))
            since = None
    if since is not None and end > since:
        out.append((since, end))
    return out


def _in_runs(moment: datetime, runs: Sequence[tuple[datetime, datetime]]) -> bool:
    return any(start <= moment < end for start, end in runs)


def _window_mean(
    periods: Mapping[datetime, float], runs: Sequence[tuple[datetime, datetime]], start: datetime, end: datetime
) -> float | None:
    """The mean kW of the 5-minute periods in `[start, end)` outside the hot-water runs, `None` below `MIN_COVER`."""
    expected = (end - start) / PERIOD
    values = [kw for moment, kw in periods.items() if start <= moment < end and not _in_runs(moment, runs)]
    if expected <= 0 or len(values) < MIN_COVER * expected:
        return None
    return sum(values) / len(values)


def _outdoor_mean(outdoor: Mapping[datetime, float], start: datetime, end: datetime) -> float | None:
    values = [value for moment, value in outdoor.items() if start <= moment < end]
    return sum(values) / len(values) if values else None


def measure_block(
    block: tuple[datetime, datetime],
    periods: Mapping[datetime, float],
    runs: Sequence[tuple[datetime, datetime]],
    *,
    next_start: datetime | None = None,
    outdoor: Mapping[datetime, float] | None = None,
) -> BlockResult | None:
    """The block measured on the meter's 5-minute means (kW by period start), `None` when it cannot be (see the
    module). `runs` are the hot-water runs, `next_start` the next block's start, `outdoor` hourly means (°C)."""
    start, end = block
    if end - start < MIN_BLOCK:
        return None
    if next_start is not None and next_start < end + AFTER:
        return None
    baseline = _window_mean(periods, runs, start - BEFORE, start)
    during = _window_mean(periods, runs, start, end)
    after = _window_mean(periods, runs, end, end + AFTER)
    if baseline is None or during is None or after is None:
        return None
    outdoor_c = None
    if outdoor:
        before_c = _outdoor_mean(outdoor, start - BEFORE, start)
        after_c = _outdoor_mean(outdoor, end, end + AFTER)
        if before_c is not None and after_c is not None and abs(after_c - before_c) > MAX_OUTDOOR_MOVE_C:
            return None
        outdoor_c = _outdoor_mean(outdoor, start - BEFORE, end + AFTER)
    return BlockResult(start, end, baseline, during, after, outdoor_c)


def learn(results: Sequence[BlockResult]) -> Effect:
    """The effect learned from the measured blocks (see the module)."""
    shares = [share for result in results if (share := result.share()) is not None]
    rebounds = [share for result in results if (share := result.rebound_share()) is not None]
    lower = (sum(shares) + PRIOR_WEIGHT * PRIOR_SHARE) / (len(shares) + PRIOR_WEIGHT)
    rebound = (sum(rebounds) + PRIOR_WEIGHT * PRIOR_REBOUND) / (len(rebounds) + PRIOR_WEIGHT)
    return Effect(_clamp(lower, SHARE_RANGE), _clamp(rebound, REBOUND_RANGE), len(shares))


def hour_of(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def hour_shares(start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """The clock hours `[start, end)` covers, each with the hours of it covered (0..1)."""
    out: list[tuple[datetime, float]] = []
    hour = hour_of(start)
    while hour < end:
        overlap = min(end, hour + HOUR) - max(start, hour)
        if overlap > timedelta(0):
            out.append((hour, _hours(overlap)))
        hour += HOUR
    return out


def forecast_delta(
    intervals: Sequence[tuple[datetime, datetime]],
    heat_kw: Callable[[datetime], float | None],
    effect: Effect,
    current_hour: datetime,
) -> dict[datetime, float]:
    """The change to the house's expected load per hour (kW, UTC hour starts) from `current_hour` on (see the
    module): negative in the lowered hours, positive in the rebound after. An hour without a heat figure counts
    nothing taken away there."""
    delta: dict[datetime, float] = {}
    for start, end in intervals:
        saved = 0.0
        for hour, share in hour_shares(start, end):
            kw = heat_kw(hour)
            if kw is None or kw <= 0:
                continue
            taken = effect.lower_share * kw * share
            saved += taken
            if hour >= current_hour:
                delta[hour] = delta.get(hour, 0.0) - taken
        back = effect.rebound * saved
        if back <= 0:
            continue
        for hour, share in hour_shares(end, end + AFTER):
            if hour >= current_hour:
                delta[hour] = delta.get(hour, 0.0) + back * share / _hours(AFTER)
    return delta
