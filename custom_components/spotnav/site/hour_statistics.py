"""A site's hours from Home Assistant's long-term statistics: a suggested import target, and the power view's
earlier months.

SpotNav measures a site's hours itself only from the day it is installed (`site/demand_tracker.py`). Home
Assistant's recorder has kept hourly statistics of the same sensors for longer: this module reads them, on request
(the import target's editor asks for a suggestion) and once to back-fill the power view. Nothing here decides
anything a charger is told, and an hour read from the statistics lifts the import target
(`planning/demand_charge.free_level_hours`) only when it is shown complete (below).

* **What is read**: the hourly statistics of the site's grid power (a direct site's total grid power, one signed
  entity or an import/export pair, negated where the site says the meter is export-positive; a derived site's
  per-phase power, less each phase's export entity) and, per member charger, what it drew: its energy register's
  hourly `change` (kWh in an hour is its mean kW), else its power sensor's hourly `mean`, else its measured current's
  `mean` per phase times `NOMINAL_VOLTAGE_V`. Only entities with long-term statistics (a `measurement` sensor has a
  mean, a `total`/`total_increasing` one a sum) and a unit this module knows (W, kW, MW; A, mA; Wh, kWh, MWh) are
  read; each is converted to W here. A charger with none is named (`chargers_without_statistics`) and the grid
  alone stands for the house.
* **An hour** is there when every grid entity has it. Its grid mean is signed (export negative); the chargers' sum
  is `None` where one with statistics lacks the hour. The house is `max(0, grid - chargers)`, or `max(0, grid)`
  where the chargers are not known: a mean within the hour, so solar that exported in one half and was imported in
  the other nets out (the documentation says so).
* **A complete hour** (`read_complete_hours`, with the back-fill only): an hour of the running month for which Home
  Assistant's 5-minute statistics (kept `purge_keep_days`, ten days by default) have all `SHORT_ROWS_PER_HOUR` rows
  for every grid entity. Its grid figure is the mean of those rows: the hourly mean, but now known to cover the whole
  hour, where an hourly mean of a sensor that was unavailable part of the hour is the mean of the rest and may be
  above what the meter billed. Only such an hour counts for the month's free level (`HourMean.complete`).
* **The suggestion** (`suggest_target`): the house's typical daily peak, the median over the days of each day's
  highest hourly mean (only within the hours a demand-charge part counts when every part has its own hours, else
  all hours), plus the car's minimum power on this site (the smallest member charger's minimum current x its phases
  x `NOMINAL_VOLTAGE_V`), rounded up to the next `SUGGEST_STEP_KW`. On the few evenings the house alone goes above
  it the car is paused, and the month's free level then lifts the target for the rest of the month: a target at
  the highest peak would pay for a level only that evening needed. Beside it the house's highest hour and what the
  car gets at the suggestion in a typical charging hour (the suggestion less the house's median hourly mean in the
  evening and night, `CHARGING_HOURS`, never below its minimum). Never invented: fewer than `MIN_DAYS` days of
  statistics, no grid statistics, or no charger on the site gives no suggestion and the reason.

Pure apart from `RecorderReader` and `site_series`; storage-free.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import math
import statistics
from typing import Any, Final, Protocol

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..planning.auto_settings import DemandComponent
from ..planning.demand_charge import counted_day, HourMean

#: The phase voltage a measured current is turned into power with (as the import target's hold does).
NOMINAL_VOLTAGE_V: Final = 230.0
#: How far back the statistics are read: three months, fewer where less exists.
PERIOD_DAYS: Final = 92
#: The fewest days of statistics a suggestion is made from.
MIN_DAYS: Final = 7
#: The suggestion is rounded up to this (kW).
SUGGEST_STEP_KW: Final = 0.5
#: The 5-minute statistics rows a complete hour has per grid entity.
SHORT_ROWS_PER_HOUR: Final = 12
#: The local hours a car typically charges in (the evening and the night): `hour >= 18 or hour < 6`.
CHARGING_FROM_HOUR: Final = 18
CHARGING_TO_HOUR: Final = 6

#: Why there is no suggestion.
REASON_NO_RECORDER: Final = "no_recorder"
REASON_NO_GRID_STATISTICS: Final = "no_grid_statistics"
REASON_TOO_FEW_DAYS: Final = "too_few_days"
REASON_NO_CHARGERS: Final = "no_chargers"

KIND_POWER: Final = "power"
KIND_CURRENT: Final = "current"
KIND_ENERGY: Final = "energy"

#: Per kind, a unit's factor to W (an energy's per hour: Wh in an hour is its mean W).
UNIT_FACTORS: Final[dict[str, dict[str, float]]] = {
    KIND_POWER: {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0},
    KIND_CURRENT: {"A": NOMINAL_VOLTAGE_V, "mA": NOMINAL_VOLTAGE_V / 1000.0},
    KIND_ENERGY: {"Wh": 1.0, "kWh": 1000.0, "MWh": 1_000_000.0},
}


@dataclass(frozen=True, slots=True)
class Term:
    """One entity read as power: `kind` says which statistic (`mean` for power and current, `change` for energy)."""

    entity_id: str
    kind: str = KIND_POWER
    sign: float = 1.0


@dataclass(frozen=True, slots=True)
class ChargerSeries:
    """One member charger's draw: its terms (summed), what they are (`source`: power, energy, current, `None` for
    none), and its minimum power on the site (kW)."""

    charger_entry_id: str
    terms: tuple[Term, ...]
    source: str | None
    min_kw: float


@dataclass(frozen=True, slots=True)
class SiteSeries:
    """What a site's statistics are read from (`site_series`)."""

    grid: tuple[Term, ...]
    chargers: tuple[ChargerSeries, ...] = ()

    @property
    def car_min_kw(self) -> float | None:
        """The smallest member charger's minimum power (kW), `None` without a charger."""
        return min((charger.min_kw for charger in self.chargers), default=None)


@dataclass(frozen=True, slots=True)
class StatMeta:
    """What the recorder keeps of an entity: its statistics' unit, and whether it has a mean and a sum."""

    unit: str | None
    has_mean: bool
    has_sum: bool


class StatisticsReader(Protocol):
    """The recorder, or a fake in the tests."""

    async def metadata(self, entity_ids: set[str]) -> dict[str, StatMeta]: ...

    async def rows(
        self, entity_ids: set[str], start: datetime, end: datetime, types: set[str]
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]: ...

    async def short_rows(
        self, entity_ids: set[str], start: datetime, end: datetime
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]: ...


@dataclass(frozen=True, slots=True)
class StatHour:
    """One clock hour from the statistics: its start (UTC), the grid's mean (kW, signed) and the chargers' (`None`
    where one with statistics lacks the hour)."""

    start: datetime
    grid_kw: float
    car_kw: float | None

    @property
    def house_kw(self) -> float:
        """The house alone (kW): the grid less the chargers, the grid alone where they are not known."""
        return max(0.0, self.grid_kw - (self.car_kw or 0.0))

    @property
    def known_house_kw(self) -> float | None:
        """The house alone where the chargers are known, else `None` (the power view's house part)."""
        return None if self.car_kw is None else max(0.0, self.grid_kw - self.car_kw)


@dataclass(frozen=True, slots=True)
class SiteHours:
    """`read_site_hours`: the hours, and what could not be read."""

    hours: tuple[StatHour, ...] = ()
    #: Whether the grid's statistics can be read (a grid entity, each with statistics in a unit this module knows).
    grid_readable: bool = True
    #: The grid entities whose statistics cannot be read.
    grid_missing: tuple[str, ...] = ()
    chargers_without_statistics: tuple[str, ...] = ()
    #: Per charger, the source read (`power`, `energy`, `current`) or `None`.
    charger_sources: dict[str, str | None] = field(default_factory=dict)


def _row_start(row: Mapping[str, Any]) -> datetime | None:
    start = row.get("start")
    if isinstance(start, (int, float)) and not isinstance(start, bool):
        return dt_util.utc_from_timestamp(start / 1000.0 if start > 1e11 else start)
    if isinstance(start, datetime):
        return dt_util.as_utc(start)
    return None


def _usable(meta: StatMeta | None, kind: str) -> float | None:
    """The factor to W for an entity of `kind`, or `None` when its statistics cannot be read as one."""
    if meta is None or meta.unit is None:
        return None
    if kind == KIND_ENERGY and not meta.has_sum:
        return None
    if kind != KIND_ENERGY and not meta.has_mean:
        return None
    return UNIT_FACTORS[kind].get(meta.unit)


def term_watts(term: Term, factor: float, rows: Iterable[Mapping[str, Any]]) -> dict[datetime, float]:
    """One entity's hours as W (signed by `term.sign`); a row without a finite figure, or off the hour, is dropped."""
    key = "change" if term.kind == KIND_ENERGY else "mean"
    found: dict[datetime, float] = {}
    for row in rows:
        start = _row_start(row)
        value = row.get(key)
        if start is None or start.minute or start.second or isinstance(value, bool):
            continue
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        found[start] = term.sign * float(value) * factor
    return found


def term_five_minutes(term: Term, factor: float, rows: Iterable[Mapping[str, Any]]) -> dict[datetime, float]:
    """One power entity's 5-minute means as W (signed by `term.sign`); a row without a finite figure, or off the five
    minutes, is dropped."""
    found: dict[datetime, float] = {}
    for row in rows:
        start = _row_start(row)
        value = row.get("mean")
        if start is None or start.minute % 5 or start.second or start.microsecond or isinstance(value, bool):
            continue
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        found[start] = term.sign * float(value) * factor
    return found


def _summed(series: Sequence[dict[datetime, float]]) -> dict[datetime, float]:
    """The hours every series has, summed."""
    if not series:
        return {}
    common = set(series[0])
    for other in series[1:]:
        common &= set(other)
    return {start: sum(item[start] for item in series) for start in common}


async def read_site_hours(reader: StatisticsReader, site: SiteSeries, start: datetime, end: datetime) -> SiteHours:
    """The site's hours between `start` and `end` (see the module)."""
    terms = [*site.grid, *(term for charger in site.chargers for term in charger.terms)]
    ids = {term.entity_id for term in terms}
    meta = await reader.metadata(ids) if ids else {}
    factors = {term: _usable(meta.get(term.entity_id), term.kind) for term in terms}
    grid_missing = tuple(term.entity_id for term in site.grid if factors[term] is None)
    sources = {charger.charger_entry_id: charger.source for charger in site.chargers}
    readable_chargers = [
        charger for charger in site.chargers if charger.terms and all(factors[term] is not None for term in charger.terms)
    ]
    without = tuple(
        charger.charger_entry_id for charger in site.chargers if charger not in readable_chargers
    )
    if not site.grid or grid_missing:
        return SiteHours(
            grid_readable=False,
            grid_missing=grid_missing,
            chargers_without_statistics=without,
            charger_sources=sources,
        )
    wanted = [*site.grid, *(term for charger in readable_chargers for term in charger.terms)]
    types = {"change" if term.kind == KIND_ENERGY else "mean" for term in wanted}
    rows = await reader.rows({term.entity_id for term in wanted}, start, end, types)

    def watts(term: Term) -> dict[datetime, float]:
        factor = factors[term]
        assert factor is not None
        return term_watts(term, factor, rows.get(term.entity_id, ()))

    grid = _summed([watts(term) for term in site.grid])
    chargers = [_summed([watts(term) for term in charger.terms]) for charger in readable_chargers]
    hours = []
    for moment in sorted(grid):
        if not start <= moment < end:
            continue
        if not site.chargers:
            car: float | None = 0.0
        elif without or any(moment not in charger for charger in chargers):
            car = None
        else:
            car = sum(charger[moment] for charger in chargers) / 1000.0
        hours.append(StatHour(start=moment, grid_kw=grid[moment] / 1000.0, car_kw=car))
    return SiteHours(hours=tuple(hours), chargers_without_statistics=without, charger_sources=sources)


async def read_complete_hours(
    reader: StatisticsReader, site: SiteSeries, start: datetime, end: datetime
) -> dict[datetime, float]:
    """The complete hours between `start` and `end` (see the module): each hour's start (UTC) and the grid's mean
    over its twelve 5-minute rows (kW, signed). Empty where the grid cannot be read."""
    if not site.grid or any(term.kind != KIND_POWER for term in site.grid):
        return {}
    ids = {term.entity_id for term in site.grid}
    meta = await reader.metadata(ids)
    factors = {term: _usable(meta.get(term.entity_id), term.kind) for term in site.grid}
    if any(factor is None for factor in factors.values()):
        return {}
    rows = await reader.short_rows(ids, start, end)
    per_term = []
    for term in site.grid:
        factor = factors[term]
        assert factor is not None
        per_term.append(term_five_minutes(term, factor, rows.get(term.entity_id, ())))
    slots = _summed(per_term)
    by_hour: dict[datetime, list[float]] = {}
    for moment, watts in slots.items():
        if start <= moment < end:
            by_hour.setdefault(moment.replace(minute=0), []).append(watts)
    return {
        hour: sum(values) / len(values) / 1000.0
        for hour, values in sorted(by_hour.items())
        if len(values) == SHORT_ROWS_PER_HOUR
    }


# ------------------------------------------------------------------------------------------------ the suggestion


@dataclass(frozen=True, slots=True)
class MonthFigures:
    """One month of the house's hours: its highest hour and the typical daily peak (the median)."""

    month: tuple[int, int]
    peak_kw: float
    peak_start: datetime
    typical_kw: float
    days: int


@dataclass(frozen=True, slots=True)
class Suggestion:
    """`suggest_target` (see the module). Without `reason` every figure is there."""

    reason: str | None
    #: Whether the daily peaks are taken within the demand-charge parts' own hours (`windows`) or over all (`all`).
    counted: str = "all"
    days: int = 0
    hours: int = 0
    typical_kw: float | None = None
    highest_kw: float | None = None
    highest_start: datetime | None = None
    car_min_kw: float | None = None
    suggestion_kw: float | None = None
    car_kw: float | None = None
    months: tuple[MonthFigures, ...] = ()


def _counting(components: Sequence[DemandComponent]) -> tuple[DemandComponent, ...] | None:
    """The parts whose hours the daily peaks are taken within, `None` for all hours (no part, or one counting all)."""
    if not components or any(component.window is None for component in components):
        return None
    return tuple(components)


def _counted_day(counting: tuple[DemandComponent, ...] | None, start: datetime, tz: str) -> date | None:
    if counting is None:
        return start.astimezone(time_zones.require_zone(tz)).date()
    for component in counting:
        day = counted_day(component, start, tz)
        if day is not None:
            return day
    return None


def _daily_peaks(hours: Iterable[StatHour], counting: tuple[DemandComponent, ...] | None, tz: str) -> dict[date, StatHour]:
    peaks: dict[date, StatHour] = {}
    for hour in hours:
        day = _counted_day(counting, hour.start, tz)
        if day is None:
            continue
        best = peaks.get(day)
        if best is None or hour.house_kw > best.house_kw:
            peaks[day] = hour
    return peaks


def round_up(kw: float, step: float = SUGGEST_STEP_KW) -> float:
    """`kw` rounded up to the next `step` (a figure already on one stays)."""
    return math.ceil(kw / step - 1e-9) * step


def suggest_target(
    hours: Sequence[StatHour],
    *,
    tz: str,
    components: Sequence[DemandComponent],
    car_min_kw: float | None,
) -> Suggestion:
    """The suggested import target from the house's hours (see the module)."""
    counting = _counting(components)
    counted = "all" if counting is None else "windows"
    peaks = _daily_peaks(hours, counting, tz)
    if car_min_kw is None:
        return Suggestion(reason=REASON_NO_CHARGERS, counted=counted, days=len(peaks), hours=len(hours))
    if len(peaks) < MIN_DAYS:
        return Suggestion(reason=REASON_TOO_FEW_DAYS, counted=counted, days=len(peaks), hours=len(hours))
    zone = time_zones.require_zone(tz)
    typical = statistics.median(hour.house_kw for hour in peaks.values())
    highest = max(peaks.values(), key=lambda hour: (hour.house_kw, -hour.start.timestamp()))
    suggestion = round_up(typical + car_min_kw)
    evening = [
        hour.house_kw
        for hour in hours
        if not CHARGING_TO_HOUR <= hour.start.astimezone(zone).hour < CHARGING_FROM_HOUR
    ]
    house_evening = statistics.median(evening) if evening else typical
    by_month: dict[tuple[int, int], dict[date, StatHour]] = {}
    for day, hour in peaks.items():
        local = hour.start.astimezone(zone)
        by_month.setdefault((local.year, local.month), {})[day] = hour
    months = tuple(
        MonthFigures(
            month=month,
            peak_kw=max(hour.house_kw for hour in days.values()),
            peak_start=max(days.values(), key=lambda hour: (hour.house_kw, -hour.start.timestamp())).start,
            typical_kw=statistics.median(hour.house_kw for hour in days.values()),
            days=len(days),
        )
        for month, days in sorted(by_month.items())
    )
    return Suggestion(
        reason=None,
        counted=counted,
        days=len(peaks),
        hours=len(hours),
        typical_kw=typical,
        highest_kw=highest.house_kw,
        highest_start=highest.start,
        car_min_kw=car_min_kw,
        suggestion_kw=suggestion,
        car_kw=max(car_min_kw, suggestion - house_evening),
        months=months,
    )


# --------------------------------------------------------------------------------------------------- back-fill


def backfill_hours(
    stat_hours: Iterable[StatHour], complete: Mapping[datetime, float] | None = None
) -> list[HourMean]:
    """Statistics hours as the hour tracker keeps them, marked `imported`: the grid's mean as import
    (`max(0, grid)`, also as the hour's floor), the house where the chargers are known. Never incomplete: an hour
    the statistics lack is simply not there. An hour in `complete` (`read_complete_hours`) is marked `complete`, at
    its twelve 5-minute rows' mean (its house part too: that mean less the chargers' hourly figure)."""
    complete = complete or {}
    hours = []
    for hour in stat_hours:
        grid_kw = complete.get(hour.start, hour.grid_kw)
        house_kw = None if hour.car_kw is None else max(0.0, grid_kw - hour.car_kw)
        hours.append(
            HourMean(
                start=hour.start,
                kw=max(0.0, grid_kw),
                house_kw=house_kw,
                floor_kw=max(0.0, grid_kw),
                imported=True,
                complete=hour.start in complete,
            )
        )
    return hours


# --------------------------------------------------------------------------------------------- Home Assistant


class RecorderReader:
    """The recorder's statistics (the long-term hourly ones, and the 5-minute ones for a complete hour), read in its
    executor."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def metadata(self, entity_ids: set[str]) -> dict[str, StatMeta]:
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import get_metadata  # noqa: PLC0415

        found = await get_instance(self.hass).async_add_executor_job(
            lambda: get_metadata(self.hass, statistic_ids=entity_ids)
        )
        answer: dict[str, StatMeta] = {}
        for entity_id, (_id, meta) in found.items():
            mean_type = meta.get("mean_type")
            has_mean = bool(meta.get("has_mean")) or bool(mean_type is not None and int(mean_type) == 1)
            answer[entity_id] = StatMeta(
                unit=meta.get("unit_of_measurement"), has_mean=has_mean, has_sum=bool(meta.get("has_sum"))
            )
        return answer

    async def rows(
        self, entity_ids: set[str], start: datetime, end: datetime, types: set[str]
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        return await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, end, entity_ids, "hour", None, types
        )

    async def short_rows(
        self, entity_ids: set[str], start: datetime, end: datetime
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        return await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, end, entity_ids, "5minute", None, {"mean"}
        )


def recorder_ready(hass: HomeAssistant) -> bool:
    return "recorder" in hass.config.components


def statistics_period(now: datetime) -> tuple[datetime, datetime]:
    """`PERIOD_DAYS` back to the start of the running hour (the running hour has no statistics yet)."""
    end = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return end - timedelta(days=PERIOD_DAYS), end


def site_series(hass: HomeAssistant, site_data: Mapping[str, Any]) -> SiteSeries:
    """What a site's statistics are read from (see the module), from its entry's data and its chargers'."""
    from ..const import (  # noqa: PLC0415 - kept beside their only use
        CONF_CHARGER_ENTRY_IDS,
        CONF_DERIVED_ENTITIES,
        CONF_GRID_POWER_INVERTED,
        CONF_GRID_POWER_SOURCE,
        CONF_MEASURED_CURRENT_SOURCE,
        CONF_PHASE_WIRING,
        CONF_POWER_ENTITY,
        DEFAULT_MIN_CURRENT_A,
    )
    from ..runtime import controller_for  # noqa: PLC0415 - it imports the site
    from .measurement_source import grid_power_source_from_dict, source_from_dict  # noqa: PLC0415

    grid: list[Term] = []
    total = grid_power_source_from_dict(site_data.get(CONF_GRID_POWER_SOURCE))
    if total is not None:
        sign = -1.0 if site_data.get(CONF_GRID_POWER_INVERTED) else 1.0
        grid.append(Term(total.power, KIND_POWER, sign))
        if total.power_export is not None:
            grid.append(Term(total.power_export, KIND_POWER, -sign))
    else:
        derived = site_data.get(CONF_DERIVED_ENTITIES)
        if isinstance(derived, Mapping):
            for phase in ("L1", "L2", "L3"):
                entities = derived.get(phase)
                if not isinstance(entities, Mapping) or not isinstance(entities.get("power"), str):
                    grid = []
                    break
                grid.append(Term(entities["power"], KIND_POWER))
                if isinstance(entities.get("power_export"), str) and entities["power_export"]:
                    grid.append(Term(entities["power_export"], KIND_POWER, -1.0))
    chargers: list[ChargerSeries] = []
    wiring_all = site_data.get(CONF_PHASE_WIRING) or {}
    for charger_entry_id in site_data.get(CONF_CHARGER_ENTRY_IDS) or []:
        wiring = wiring_all.get(charger_entry_id) or {} if isinstance(wiring_all, Mapping) else {}
        phases = wiring.get("phases") if wiring.get("phases") in (1, 3) else 3
        min_a = wiring.get("min_current_a", DEFAULT_MIN_CURRENT_A)
        min_a = float(min_a) if isinstance(min_a, (int, float)) and not isinstance(min_a, bool) else DEFAULT_MIN_CURRENT_A
        min_kw = min_a * phases * NOMINAL_VOLTAGE_V / 1000.0
        terms: tuple[Term, ...] = ()
        source: str | None = None
        controller = controller_for(hass, charger_entry_id)
        register = None if controller is None else getattr(controller, "energy_register_entity_id", None)
        entry = hass.config_entries.async_get_entry(charger_entry_id)
        power = None if entry is None else entry.data.get(CONF_POWER_ENTITY)
        measured = source_from_dict(wiring.get(CONF_MEASURED_CURRENT_SOURCE))
        if isinstance(register, str) and register:
            terms, source = (Term(register, KIND_ENERGY),), KIND_ENERGY
        elif isinstance(power, str) and power:
            terms, source = (Term(power, KIND_POWER),), KIND_POWER
        elif measured is not None and measured.kind == "separate_entities" and measured.entity_ids:
            terms = tuple(Term(entity_id, KIND_CURRENT) for entity_id in dict(measured.entity_ids).values())
            source = KIND_CURRENT
        chargers.append(ChargerSeries(charger_entry_id, terms, source, min_kw))
    return SiteSeries(grid=tuple(grid), chargers=tuple(chargers))
