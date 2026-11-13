"""Solar forecast correction with guard rails (SpotNav 3.0, wave 2). Pure: no Home Assistant, the clock passed in.

The sun's forecast (the Energy dashboard's forecast sources, `planning/hybrid_forecast.py`) is often off in a
steady way for one site: shade, soiling, a tilt set wrong, a provider that is optimistic under clouds. This learns
one factor per *kind of hour* and multiplies the forecast by it:

* **Kinds**: `cloudy` when the weather entity's hourly forecast gave the hour a cloud cover of at least
  `CLOUDY_PCT` %, else `clear` (also when no cloud cover is known).
* **Factor**: the outcome's ratio to the forecast over the last `WINDOW_DAYS` days, recency weighted (yesterday
  1.0, then `WEIGHT_FALL` less per day, never below `WEIGHT_FLOOR`), only over hours whose forecast is above
  `MIN_FORECAST_KWH`, and only once `MIN_DAYS` days have such hours of that kind. Once per local day the factor
  moves `STEP_SHARE` (a fifth) of the way towards that ratio; it stays within `FACTOR_MIN`-`FACTOR_MAX`.

Guard rails (Predbat's, which it learned the hard way):

* **Down days** are skipped: a day whose production was below `DOWN_SHARE` of its forecast had the system off,
  not a bad forecast.
* **Clipping**: an hour where production sat flat at the inverter's ceiling is counted as if the forecast had been
  right (imputed), so an inverter limit is not learned as a weak forecast. The ceiling is the inverter's limit when
  it is known, else a plateau seen in the 5-minute production (`plateau_ceiling`).
* **Caps**: the corrected forecast never exceeds the larger of the raw forecast and the highest hour ever observed,
  nor the array's size where a forecast source states it. So a factor cannot inflate a 6 kWp array to 16 kW.
* **Never from a corrected series**: the samples are built from the raw forecast's log only (`samples_from_log`
  takes the raw kind), never from what this module produced.
* **Curtailed sites** are left alone (`curtailment`): an export limit seen as a plateau in the export, a site that
  exports nothing, or production that falls away in negative-price hours. While curtailment is visible the
  correction is off (the raw forecast is passed through) and the factors do not move. Negative-price hours are
  never learned from either way.

In this release the result is shadow only: it is logged beside the raw forecast and both are scored by the nightly
review; nothing that plans or controls reads it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Final

from .. import time_zones

KIND_CLEAR: Final = "clear"
KIND_CLOUDY: Final = "cloudy"
KINDS: Final = (KIND_CLEAR, KIND_CLOUDY)

#: An hour with at least this cloud cover (%) is a cloudy one.
CLOUDY_PCT: Final = 60.0
FACTOR_MIN: Final = 0.3
FACTOR_MAX: Final = 2.0
#: Once a day the factor moves this share of the way towards the window's ratio.
STEP_SHARE: Final = 0.2
#: Only hours whose forecast is above this (kWh) are learned from.
MIN_FORECAST_KWH: Final = 0.2
WINDOW_DAYS: Final = 7
WEIGHT_FALL: Final = 0.1
WEIGHT_FLOOR: Final = 0.3
#: A kind's factor moves only when this many days in the window have hours of that kind.
MIN_DAYS: Final = 3
#: A day whose production was below this share of its forecast had the system down.
DOWN_SHARE: Final = 0.1

#: A reading within this share of the ceiling is at it.
CLIP_TOLERANCE: Final = 0.01
#: A plateau is this many 5-minute slots in a row at the ceiling ...
PLATEAU_SLOTS: Final = 9
#: ... seen on at least this many days (a natural peak is not that flat that often).
PLATEAU_DAYS: Final = 2
#: An hour with at least this many 5-minute slots at the ceiling was clipped.
CLIPPED_HOUR_SLOTS: Final = 3
#: The smallest production plateau taken as an inverter ceiling (kW).
MIN_CEILING_KW: Final = 0.5
#: The smallest export plateau taken as an export limit (kW); a zero-export site is caught by `ZERO_EXPORT_SHARE`.
MIN_EXPORT_LIMIT_KW: Final = 0.3
#: A site whose export was at most this share of its production (over at least `ZERO_EXPORT_MIN_KWH`) is zero-export.
ZERO_EXPORT_SHARE: Final = 0.01
ZERO_EXPORT_MIN_KWH: Final = 10.0
#: Production in negative-price hours below this share of the same day's other hours' ratio is curtailment.
NEGATIVE_DROP_SHARE: Final = 0.5
#: Kept for the diagnostics: this many days of the factors.
HISTORY_DAYS: Final = 30
#: The per-hour facts (cloud cover, negative price) are kept as long as the log keeps its hours.
META_DAYS: Final = 14

SLOT: Final = timedelta(minutes=5)

CURTAILED_EXPORT_LIMIT: Final = "export_limit"
CURTAILED_ZERO_EXPORT: Final = "zero_export"
CURTAILED_NEGATIVE_PRICE: Final = "negative_price"

STATUS_LEARNING: Final = "learning"
STATUS_SHADOW: Final = "shadow"
STATUS_OFF_CURTAILED: Final = "off_curtailed"


def kind_of(cloud_pct: float | None) -> str:
    """The kind of an hour with `cloud_pct` % cloud cover (`clear` when it is not known)."""
    return KIND_CLOUDY if cloud_pct is not None and cloud_pct >= CLOUDY_PCT else KIND_CLEAR


def clamp_factor(value: float) -> float:
    return min(FACTOR_MAX, max(FACTOR_MIN, value))


def day_weight(age_days: int) -> float:
    """Yesterday (age 1) weighs 1.0, each older day `WEIGHT_FALL` less, never below `WEIGHT_FLOOR`."""
    return max(WEIGHT_FLOOR, 1.0 - WEIGHT_FALL * (age_days - 1))


# ------------------------------------------------------------------------------------------------ plateaus


def _runs(slots: Mapping[datetime, float], level: float) -> list[tuple[datetime, int]]:
    """The runs of consecutive 5-minute slots at or above `level`: `(first slot, length)`."""
    runs: list[tuple[datetime, int]] = []
    first: datetime | None = None
    previous: datetime | None = None
    length = 0
    for start in sorted(slots):
        at = slots[start] >= level
        if at and first is not None and previous is not None and start - previous == SLOT:
            length += 1
        elif at:
            if first is not None:
                runs.append((first, length))
            first, length = start, 1
        elif first is not None:
            runs.append((first, length))
            first, length = None, 0
        previous = start
    if first is not None:
        runs.append((first, length))
    return runs


def plateau_ceiling(slots: Mapping[datetime, float], *, min_kw: float) -> float | None:
    """The level a 5-minute power series (`{slot start: kW}`) is held at, `None` when it shows no such ceiling.

    The series' highest reading is a ceiling when the series sat within `CLIP_TOLERANCE` of it for at least
    `PLATEAU_SLOTS` slots in a row on at least `PLATEAU_DAYS` different days, and it is at least `min_kw`.
    """
    values = [value for value in slots.values() if _number(value)]
    if not values:
        return None
    peak = max(values)
    if peak < min_kw:
        return None
    days = {first.date() for first, length in _runs(slots, peak * (1.0 - CLIP_TOLERANCE)) if length >= PLATEAU_SLOTS}
    return peak if len(days) >= PLATEAU_DAYS else None


def clipped_hours(slots: Mapping[datetime, float], ceiling_kw: float | None) -> set[datetime]:
    """The hours (UTC starts) with at least `CLIPPED_HOUR_SLOTS` 5-minute slots at `ceiling_kw`."""
    if ceiling_kw is None or ceiling_kw <= 0:
        return set()
    level = ceiling_kw * (1.0 - CLIP_TOLERANCE)
    counts: dict[datetime, int] = {}
    for start, value in slots.items():
        if _number(value) and value >= level:
            hour = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
            counts[hour] = counts.get(hour, 0) + 1
    return {hour for hour, count in counts.items() if count >= CLIPPED_HOUR_SLOTS}


# ------------------------------------------------------------------------------------------------ evidence


@dataclass(frozen=True, slots=True)
class HourSample:
    """One completed hour: the raw first forecast and the outcome (kWh), and what is known of it."""

    start: datetime
    forecast_kwh: float
    actual_kwh: float
    cloudy: bool = False
    clipped: bool = False
    negative_price: bool = False


@dataclass(frozen=True, slots=True)
class DayEvidence:
    """One local day's usable hours per kind, after the guard rails."""

    day: date
    down: bool
    forecast: dict[str, float]
    actual: dict[str, float]
    hours: dict[str, int]
    imputed: int = 0
    negative_hours: int = 0
    negative_curtailed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "down": self.down,
            "forecast_kwh": {kind: round(value, 3) for kind, value in self.forecast.items()},
            "actual_kwh": {kind: round(value, 3) for kind, value in self.actual.items()},
            "hours": dict(self.hours),
            "imputed": self.imputed,
            "negative_hours": self.negative_hours,
            "negative_curtailed": self.negative_curtailed,
        }


def _ratio(pairs: Iterable[tuple[float, float]]) -> float | None:
    forecast = actual = 0.0
    for f, a in pairs:
        forecast += f
        actual += a
    return None if forecast <= 0 else actual / forecast


def day_evidence(samples: Iterable[HourSample], tz: str) -> dict[date, DayEvidence]:
    """The samples per local day, with the guard rails applied (see the module)."""
    zone = time_zones.require_zone(tz)
    by_day: dict[date, list[HourSample]] = {}
    for sample in samples:
        if sample.forecast_kwh > MIN_FORECAST_KWH and _number(sample.actual_kwh) and sample.actual_kwh >= 0:
            by_day.setdefault(sample.start.astimezone(zone).date(), []).append(sample)
    evidence: dict[date, DayEvidence] = {}
    for day, rows in by_day.items():
        priced = [row for row in rows if not row.negative_price]
        negative = [row for row in rows if row.negative_price]
        other_ratio = _ratio((row.forecast_kwh, row.actual_kwh) for row in priced)
        negative_ratio = _ratio((row.forecast_kwh, row.actual_kwh) for row in negative)
        negative_curtailed = (
            other_ratio is not None
            and negative_ratio is not None
            and negative_ratio < NEGATIVE_DROP_SHARE * other_ratio
        )
        down = other_ratio is not None and other_ratio < DOWN_SHARE
        forecast = dict.fromkeys(KINDS, 0.0)
        actual = dict.fromkeys(KINDS, 0.0)
        hours = dict.fromkeys(KINDS, 0)
        imputed = 0
        for row in priced:
            kind = KIND_CLOUDY if row.cloudy else KIND_CLEAR
            outcome = row.actual_kwh
            if row.clipped and outcome < row.forecast_kwh:
                # The inverter held production down: the forecast may well have been right.
                outcome = row.forecast_kwh
                imputed += 1
            forecast[kind] += row.forecast_kwh
            actual[kind] += outcome
            hours[kind] += 1
        evidence[day] = DayEvidence(
            day=day,
            down=down,
            forecast=forecast,
            actual=actual,
            hours=hours,
            imputed=imputed,
            negative_hours=len(negative),
            negative_curtailed=negative_curtailed,
        )
    return evidence


def window_targets(evidence: Mapping[date, DayEvidence], today: date) -> dict[str, tuple[float | None, int]]:
    """Per kind, the window's recency-weighted ratio (`None` until `MIN_DAYS` days have its hours) and its days."""
    targets: dict[str, tuple[float | None, int]] = {}
    for kind in KINDS:
        forecast = actual = 0.0
        days = 0
        for day, item in evidence.items():
            age = (today - day).days
            if not 1 <= age <= WINDOW_DAYS or item.down or item.hours.get(kind, 0) == 0:
                continue
            weight = day_weight(age)
            forecast += weight * item.forecast[kind]
            actual += weight * item.actual[kind]
            days += 1
        ratio = actual / forecast if days >= MIN_DAYS and forecast > 0 else None
        targets[kind] = (ratio, days)
    return targets


def curtailment(
    evidence: Mapping[date, DayEvidence],
    today: date,
    *,
    export_slots: Mapping[datetime, float] | None,
    solar_kwh: float | None,
    export_kwh: float | None,
) -> str | None:
    """Why the site's production looks curtailed in the window, `None` when it does not (see the module).

    `export_slots` is the grid export's 5-minute power (kW), `solar_kwh` and `export_kwh` the window's totals; any
    of them `None` when the site does not measure it (that test is then skipped).
    """
    if export_slots and plateau_ceiling(export_slots, min_kw=MIN_EXPORT_LIMIT_KW) is not None:
        return CURTAILED_EXPORT_LIMIT
    if (
        solar_kwh is not None
        and export_kwh is not None
        and solar_kwh >= ZERO_EXPORT_MIN_KWH
        and export_kwh <= ZERO_EXPORT_SHARE * solar_kwh
    ):
        return CURTAILED_ZERO_EXPORT
    for day, item in evidence.items():
        if 1 <= (today - day).days <= WINDOW_DAYS and item.negative_curtailed and not item.down:
            return CURTAILED_NEGATIVE_PRICE
    return None


# ------------------------------------------------------------------------------------------------ the state


@dataclass(slots=True)
class HourMeta:
    """What was known of an hour when its forecast was first logged (cloud cover) or since (a negative price)."""

    cloud_pct: float | None = None
    negative_price: bool | None = None


def samples_from_log(
    raw_hours: Mapping[datetime, Any],
    meta: Mapping[datetime, HourMeta],
    clipped: set[datetime],
) -> list[HourSample]:
    """The completed hours of the **raw** forecast's log (`LoggedHour`s of the raw kind) as samples. The corrected
    kind is never passed here: the correction learns from what the provider said, never from itself."""
    samples: list[HourSample] = []
    for start, hour in raw_hours.items():
        actual = getattr(hour, "actual", None)
        forecast = getattr(hour, "forecast", None)
        if actual is None or forecast is None:
            continue
        info = meta.get(start) or HourMeta()
        samples.append(
            HourSample(
                start=start,
                forecast_kwh=float(forecast),
                actual_kwh=float(actual),
                cloudy=kind_of(info.cloud_pct) == KIND_CLOUDY,
                clipped=start in clipped,
                negative_price=bool(info.negative_price),
            )
        )
    return samples


def cap_kwh(raw_kwh: float, *, observed_max_kwh: float | None, array_kwp: float | None) -> float:
    """The most a corrected hour may say: the larger of the raw forecast and the highest hour observed (only the raw
    forecast before anything was observed), and never more than the array's size for an hour."""
    cap = raw_kwh if observed_max_kwh is None else max(raw_kwh, observed_max_kwh)
    if array_kwp is not None and array_kwp > 0:
        cap = min(cap, array_kwp)
    return max(0.0, cap)


@dataclass(slots=True)
class SolarCorrection:
    """The site's factors and how they came about (see the module)."""

    factors: dict[str, float] = field(default_factory=lambda: dict.fromkeys(KINDS, 1.0))
    days: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    targets: dict[str, float | None] = field(default_factory=lambda: dict.fromkeys(KINDS))
    updated_on: date | None = None
    curtailed: str | None = None
    meta: dict[datetime, HourMeta] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.curtailed is not None:
            return STATUS_OFF_CURTAILED
        if all(self.days.get(kind, 0) < MIN_DAYS for kind in KINDS):
            return STATUS_LEARNING
        return STATUS_SHADOW

    def note_hour(self, start: datetime, *, cloud_pct: float | None, negative_price: bool | None) -> None:
        """Keep an hour's cloud cover as first seen with its forecast, and its price's sign once known."""
        info = self.meta.get(start)
        if info is None:
            info = self.meta[start] = HourMeta(cloud_pct=cloud_pct)
        if negative_price is not None:
            info.negative_price = negative_price

    def prune(self, now: datetime) -> None:
        oldest = now - timedelta(days=META_DAYS)
        self.meta = {start: info for start, info in self.meta.items() if start >= oldest}

    def nightly(self, evidence: Mapping[date, DayEvidence], today: date, *, curtailed: str | None) -> bool:
        """Once per local day: move each kind's factor a fifth of the way towards its window ratio. Off (nothing
        moves) while `curtailed`. Whether it ran (it runs once per day)."""
        if self.updated_on == today:
            return False
        self.updated_on = today
        self.curtailed = curtailed
        targets = window_targets(evidence, today)
        for kind in KINDS:
            ratio, days = targets[kind]
            self.days[kind] = days
            self.targets[kind] = ratio
            if curtailed is None and ratio is not None:
                factor = self.factors.get(kind, 1.0)
                self.factors[kind] = clamp_factor(factor + STEP_SHARE * (clamp_factor(ratio) - factor))
        self.history.append(
            {
                "date": today.isoformat(),
                "factors": {kind: round(value, 3) for kind, value in self.factors.items()},
                "targets": {kind: None if value is None else round(value, 3) for kind, value in self.targets.items()},
                "days": dict(self.days),
                "curtailed": curtailed,
            }
        )
        self.history = self.history[-HISTORY_DAYS:]
        return True

    def correct(
        self,
        raw: Mapping[datetime, float],
        *,
        observed_max_kwh: float | None,
        array_kwp: float | None,
        cloud_pct: Mapping[datetime, float] | None = None,
    ) -> dict[datetime, float]:
        """The corrected forecast (kWh per hour) for `raw`. Each hour's kind is from `cloud_pct` (the latest cloud
        forecast) when it has the hour, else from its kept cloud cover. Off while curtailed: `raw` as it is (still
        capped by the array's size, which no forecast may exceed)."""
        corrected: dict[datetime, float] = {}
        for start, value in raw.items():
            if not _number(value):
                continue
            if self.curtailed is not None:
                factor = 1.0
            elif cloud_pct is not None and start in cloud_pct:
                factor = self.factors.get(kind_of(cloud_pct[start]), 1.0)
            else:
                factor = self.factors.get(self._kind(start), 1.0)
            corrected[start] = min(
                max(0.0, value) * factor,
                cap_kwh(max(0.0, value), observed_max_kwh=observed_max_kwh, array_kwp=array_kwp),
            )
        return corrected

    def _kind(self, start: datetime) -> str:
        info = self.meta.get(start)
        return kind_of(None if info is None else info.cloud_pct)

    # ---- storage

    def as_dict(self) -> dict[str, Any]:
        return {
            "factors": {kind: round(value, 4) for kind, value in self.factors.items()},
            "days": dict(self.days),
            "targets": {kind: None if value is None else round(value, 4) for kind, value in self.targets.items()},
            "updated_on": None if self.updated_on is None else self.updated_on.isoformat(),
            "curtailed": self.curtailed,
            "meta": [
                [start.isoformat(), info.cloud_pct, info.negative_price]
                for start, info in sorted(self.meta.items())
            ],
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> SolarCorrection:
        """What the store kept; anything malformed falls back to its default."""
        state = cls()
        if not isinstance(raw, dict):
            return state
        factors = raw.get("factors")
        if isinstance(factors, dict):
            for kind in KINDS:
                value = factors.get(kind)
                if _number(value):
                    state.factors[kind] = clamp_factor(float(value))
        days = raw.get("days")
        if isinstance(days, dict):
            for kind in KINDS:
                value = days.get(kind)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    state.days[kind] = value
        targets = raw.get("targets")
        if isinstance(targets, dict):
            for kind in KINDS:
                value = targets.get(kind)
                state.targets[kind] = float(value) if _number(value) else None
        if isinstance(raw.get("updated_on"), str):
            try:
                state.updated_on = date.fromisoformat(raw["updated_on"])
            except ValueError:
                state.updated_on = None
        if raw.get("curtailed") in (CURTAILED_EXPORT_LIMIT, CURTAILED_ZERO_EXPORT, CURTAILED_NEGATIVE_PRICE):
            state.curtailed = raw["curtailed"]
        for row in raw.get("meta") or []:
            if not isinstance(row, list) or len(row) != 3 or not isinstance(row[0], str):
                continue
            try:
                start = datetime.fromisoformat(row[0])
            except ValueError:
                continue
            if start.tzinfo is None:
                continue
            cloud = float(row[1]) if _number(row[1]) else None
            negative = row[2] if isinstance(row[2], bool) else None
            state.meta[start.astimezone(timezone.utc)] = HourMeta(cloud, negative)
        history = raw.get("history")
        if isinstance(history, list):
            state.history = [item for item in history if isinstance(item, dict)][-HISTORY_DAYS:]
        return state


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
