"""The decision history's ledger (plan `plans/v3_decision_history.md`): per site, one row per load per clock hour of
what the plan said at the hour's start, what ran and why, and one row per hour for the site. Pure: no Home Assistant.
The shell (`site/decision_recorder.py`) samples the controllers once a minute and hands each sample here; the Plan
tab's past days are read from it (`site/plan_history.py`).

**The running hour** (`OpenHour`) holds what the plan said for it, taken from the plan view (`site/plan_view.py`)
once, at the hour's start (`snapshot`): each load's lane for that hour (`what`, `kw`, `reason`, `level`, whether
SpotNav only watched it) and the site's (the price, the ceiling that applied and the net from the grid the plan
counted on). A plan that changes later in the hour never changes it. Each sample adds what was measured (`Sample`):
the grid's import and the house without the cars and the battery, and per load its power, what it was doing and,
where a control decided, the reason that decided (`ceiling` for a shed step, `import_target`, `accept_peak`,
`person_stop`) or a hint for a plan that was not followed (`car_full`, `not_plugged`, `car_limited`).

**When the hour ends** (`close`) it becomes rows: the measured means over the samples taken, what ran (the most
common action held for at least a sixth of the samples) and the reason. The reason prefers what decided over the
plan's own: a decisive reason held for at least `DECISIVE_SHARE` of the samples; else the plan's reason where what ran
agrees with the plan (`agrees`); else the most common hint; else none.

**Missing hours**: an hour without a row was not recorded (Home Assistant was not running), never filled in. An hour
with fewer samples than `FULL_SHARE` of the hour's is `partial`; one whose plan was read only after a restart in it
says how many minutes late (`planned_at_min`).

**Stored** (`as_store`/`from_store`) in columns, an hour as its number since the epoch (UTC) and a load by its index
in `loads`; trimmed to the local days `KEPT_DAYS` back and today (`trim`). The size budget: a hour's site row is about
60 bytes of JSON and a load's about 55; a site with a car, a battery and heat writes about 13 kB a day, 105 kB for the
eight days kept, and never more than `MAX_LOADS` loads an hour (about 1 kB an hour, 200 kB in all).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Final

#: The store's shape; a reader checks it.
SCHEMA: Final = 1
#: The local days kept before today.
KEPT_DAYS: Final = 7
#: The samples an hour expects (one a minute).
SAMPLES_PER_HOUR: Final = 60
#: An hour with fewer samples than this share of `SAMPLES_PER_HOUR` is `partial`.
FULL_SHARE: Final = 0.9
#: What ran: an action held for at least this share of the samples.
WHAT_SHARE: Final = 1 / 6
#: A decisive reason held for at least this share of the samples (five minutes of the hour).
DECISIVE_SHARE: Final = 1 / 12
#: The loads kept per hour at most (the size budget).
MAX_LOADS: Final = 12
#: Planned and measured power agree within this (kW), or within `AGREE_SHARE` of the plan.
AGREE_KW: Final = 0.3
AGREE_SHARE: Final = 0.2
HOUR: Final = timedelta(hours=1)
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=timezone.utc)

# The decisive reasons a control reports (`Sample.reason`); `ceiling` is the plan's own code for a shed step.
REASON_CEILING: Final = "ceiling"
REASON_IMPORT_TARGET: Final = "import_target"
REASON_ACCEPT_PEAK: Final = "accept_peak"
REASON_PERSON_STOP: Final = "person_stop"
# The hints for a plan not followed (`Sample.hint`).
HINT_CAR_FULL: Final = "car_full"
HINT_NOT_PLUGGED: Final = "not_plugged"
HINT_CAR_LIMITED: Final = "car_limited"
REASONS: Final = (REASON_CEILING, REASON_IMPORT_TARGET, REASON_ACCEPT_PEAK, REASON_PERSON_STOP)
HINTS: Final = (HINT_CAR_FULL, HINT_NOT_PLUGGED, HINT_CAR_LIMITED)

RECORDED_FULL: Final = "full"
RECORDED_PARTIAL: Final = "partial"
RECORDED_MISSING: Final = "missing"

HOUR_COLUMNS: Final = (
    "hour",
    "samples",
    "planned_at_min",
    "price",
    "sell",
    "import_kw",
    "house_kw",
    "ceiling_kw",
    "ceiling",
    "planned_net_kw",
)
LOAD_COLUMNS: Final = (
    "hour",
    "load",
    "shadow",
    "planned_what",
    "planned_kw",
    "planned_reason",
    "planned_level",
    "ran_what",
    "ran_kw",
    "reason",
    "params",
)


def hour_number(moment: datetime) -> int:
    """The clock hour's number since the epoch (UTC)."""
    return int((moment.astimezone(timezone.utc) - _EPOCH).total_seconds() // 3600)


def hour_of(number: int) -> datetime:
    return _EPOCH + HOUR * number


def _r(value: float | None, digits: int = 2) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), digits)


@dataclass(slots=True)
class Planned:
    """A load's lane for one hour as the plan stood at the hour's start."""

    what: str | None
    kw: float | None
    reason: str | None
    level: int | None = None


@dataclass(slots=True)
class LoadAtStart:
    kind: str
    name: str | None
    shadow: bool
    planned: Planned


@dataclass(slots=True)
class SiteAtStart:
    price: float | None
    sell: float | None
    ceiling_kw: float | None
    ceiling: str | None
    net_kw: float | None


@dataclass(slots=True)
class LoadSample:
    """One load in one sample: its power (kW, `None` unread), what it was doing, a decisive reason, a hint."""

    kind: str
    name: str | None
    kw: float | None = None
    what: str | None = None
    reason: str | None = None
    hint: str | None = None
    params: dict[str, Any] | None = None
    shadow: bool | None = None


@dataclass(slots=True)
class Sample:
    """One minute's measurements: the grid's import and the house's without the cars and the battery (kW)."""

    grid_kw: float | None
    house_kw: float | None
    loads: dict[str, LoadSample] = field(default_factory=dict)


@dataclass(slots=True)
class _Acc:
    kind: str
    name: str | None
    kw_sum: float = 0.0
    kw_n: int = 0
    whats: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    hints: Counter[str] = field(default_factory=Counter)
    params: dict[str, dict[str, Any]] = field(default_factory=dict)
    shadow: bool | None = None

    def as_store(self) -> list[Any]:
        return [
            self.kind,
            self.name,
            round(self.kw_sum, 4),
            self.kw_n,
            dict(self.whats),
            dict(self.reasons),
            dict(self.hints),
            {key: dict(value) for key, value in self.params.items()},
            self.shadow,
        ]

    @classmethod
    def from_store(cls, raw: Any) -> _Acc | None:
        if not isinstance(raw, list) or len(raw) != 9 or not isinstance(raw[0], str):
            return None
        return cls(
            kind=raw[0],
            name=raw[1] if isinstance(raw[1], str) else None,
            kw_sum=float(raw[2]) if _number(raw[2]) else 0.0,
            kw_n=int(raw[3]) if _number(raw[3]) else 0,
            whats=_counter(raw[4]),
            reasons=_counter(raw[5]),
            hints=_counter(raw[6]),
            params={key: value for key, value in raw[7].items() if isinstance(value, dict)}
            if isinstance(raw[7], dict)
            else {},
            shadow=raw[8] if isinstance(raw[8], bool) else None,
        )


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _counter(raw: Any) -> Counter[str]:
    if not isinstance(raw, dict):
        return Counter()
    return Counter({key: int(value) for key, value in raw.items() if isinstance(key, str) and _number(value) and value > 0})


@dataclass(slots=True)
class OpenHour:
    """The running hour: its plan at the start (`None` until read) and its samples so far."""

    hour: int
    planned_at: datetime | None = None
    site: SiteAtStart | None = None
    loads: dict[str, LoadAtStart] = field(default_factory=dict)
    samples: int = 0
    grid_sum: float = 0.0
    grid_n: int = 0
    house_sum: float = 0.0
    house_n: int = 0
    acc: dict[str, _Acc] = field(default_factory=dict)

    @property
    def start(self) -> datetime:
        return hour_of(self.hour)

    def plan(self, at: datetime, site: SiteAtStart, loads: Mapping[str, LoadAtStart]) -> bool:
        """Keep the plan for this hour, once: a plan read later never replaces it. Whether it was kept."""
        if self.planned_at is not None:
            return False
        self.planned_at = at
        self.site = site
        self.loads = dict(list(loads.items())[:MAX_LOADS])
        return True

    def add(self, sample: Sample) -> None:
        self.samples += 1
        if sample.grid_kw is not None:
            self.grid_sum += sample.grid_kw
            self.grid_n += 1
        if sample.house_kw is not None:
            self.house_sum += sample.house_kw
            self.house_n += 1
        for load_id, seen in sample.loads.items():
            acc = self.acc.get(load_id)
            if acc is None:
                if len(self.acc) >= MAX_LOADS:
                    continue
                acc = self.acc[load_id] = _Acc(seen.kind, seen.name)
            acc.name = seen.name or acc.name
            if seen.shadow is not None:
                acc.shadow = seen.shadow
            if seen.kw is not None:
                acc.kw_sum += seen.kw
                acc.kw_n += 1
            if seen.what is not None:
                acc.whats[seen.what] += 1
            if seen.reason is not None:
                acc.reasons[seen.reason] += 1
                if seen.params:
                    acc.params[seen.reason] = dict(seen.params)
            if seen.hint is not None:
                acc.hints[seen.hint] += 1

    def as_store(self) -> dict[str, Any]:
        site = self.site
        return {
            "hour": self.hour,
            "planned_at": None if self.planned_at is None else self.planned_at.isoformat(),
            "site": None if site is None else [site.price, site.sell, site.ceiling_kw, site.ceiling, site.net_kw],
            "loads": {
                key: [load.kind, load.name, load.shadow, load.planned.what, load.planned.kw, load.planned.reason, load.planned.level]
                for key, load in self.loads.items()
            },
            "samples": self.samples,
            "grid": [round(self.grid_sum, 4), self.grid_n],
            "house": [round(self.house_sum, 4), self.house_n],
            "acc": {key: acc.as_store() for key, acc in self.acc.items()},
        }

    @classmethod
    def from_store(cls, raw: Any) -> OpenHour | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("hour"), int):
            return None
        opened = cls(hour=raw["hour"])
        at = raw.get("planned_at")
        opened.planned_at = _instant(at)
        site = raw.get("site")
        if isinstance(site, list) and len(site) == 5:
            opened.site = SiteAtStart(
                _num_or_none(site[0]), _num_or_none(site[1]), _num_or_none(site[2]), _text_or_none(site[3]), _num_or_none(site[4])
            )
        for key, row in (raw.get("loads") or {}).items() if isinstance(raw.get("loads"), dict) else ():
            if isinstance(key, str) and isinstance(row, list) and len(row) == 7 and isinstance(row[0], str):
                opened.loads[key] = LoadAtStart(
                    row[0],
                    _text_or_none(row[1]),
                    row[2] is True,
                    Planned(_text_or_none(row[3]), _num_or_none(row[4]), _text_or_none(row[5]), row[6] if isinstance(row[6], int) else None),
                )
        opened.samples = raw["samples"] if isinstance(raw.get("samples"), int) else 0
        for name in ("grid", "house"):
            pair = raw.get(name)
            if isinstance(pair, list) and len(pair) == 2 and _number(pair[0]) and isinstance(pair[1], int):
                setattr(opened, f"{name}_sum", float(pair[0]))
                setattr(opened, f"{name}_n", pair[1])
        for key, value in (raw.get("acc") or {}).items() if isinstance(raw.get("acc"), dict) else ():
            acc = _Acc.from_store(value)
            if acc is not None and isinstance(key, str):
                opened.acc[key] = acc
        return opened


def _num_or_none(value: Any) -> float | None:
    return float(value) if _number(value) else None


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


#: Actions that are one for the comparison: the battery charging (from the grid, the sun or either) or giving, and the
#: heat lowered (for the price, the night or away).
_SAME: Final = {
    "grid_charge": "charge",
    "sun_charge": "charge",
    "sell": "cover",
    "night": "lower",
    "away": "lower",
}


def same_action(planned: str | None, ran: str | None) -> bool:
    return (_SAME.get(planned, planned) if planned else None) == (_SAME.get(ran, ran) if ran else None)


def agrees(planned: Planned | None, ran_what: str | None, ran_kw: float | None) -> bool:
    """Whether what ran is what the plan said (the same action, the power within `AGREE_KW` or `AGREE_SHARE`)."""
    if planned is None:
        return ran_what is None
    if not same_action(planned.what, ran_what):
        return False
    if planned.kw is None or ran_kw is None:
        return True
    return abs(planned.kw - ran_kw) <= max(AGREE_KW, AGREE_SHARE * abs(planned.kw))


def _most(counter: Counter[str], least: float) -> str | None:
    if not counter:
        return None
    found, count = max(counter.items(), key=lambda item: (item[1], item[0]))
    return found if count >= least else None


@dataclass(frozen=True, slots=True)
class LoadRow:
    hour: int
    load_id: str
    kind: str
    name: str | None
    shadow: bool
    planned: Planned | None
    ran_what: str | None
    ran_kw: float | None
    reason: str | None
    params: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class HourRow:
    hour: int
    samples: int
    planned_at_min: int | None
    price: float | None
    sell: float | None
    import_kw: float | None
    house_kw: float | None
    ceiling_kw: float | None
    ceiling: str | None
    planned_net_kw: float | None

    @property
    def recorded(self) -> str:
        return RECORDED_FULL if self.samples >= FULL_SHARE * SAMPLES_PER_HOUR else RECORDED_PARTIAL


def close(opened: OpenHour, extra_kw: Mapping[str, float | None] | None = None) -> tuple[HourRow, list[LoadRow]]:
    """The ended hour as rows (see the module). `extra_kw`: a load's power for the hour read at its end (the heat
    meter's hour), in place of its samples."""
    site = opened.site
    planned_at_min = (
        None
        if opened.planned_at is None
        else max(0, int((opened.planned_at - opened.start).total_seconds() // 60))
    )
    hour_row = HourRow(
        hour=opened.hour,
        samples=opened.samples,
        planned_at_min=planned_at_min,
        price=None if site is None else site.price,
        sell=None if site is None else site.sell,
        import_kw=_r(opened.grid_sum / opened.grid_n) if opened.grid_n else None,
        house_kw=_r(opened.house_sum / opened.house_n) if opened.house_n else None,
        ceiling_kw=None if site is None else site.ceiling_kw,
        ceiling=None if site is None else site.ceiling,
        planned_net_kw=None if site is None else site.net_kw,
    )
    rows: list[LoadRow] = []
    ids = list(opened.loads)
    ids.extend(key for key in opened.acc if key not in opened.loads)
    for load_id in ids[:MAX_LOADS]:
        start = opened.loads.get(load_id)
        acc = opened.acc.get(load_id)
        kind = start.kind if start is not None else acc.kind  # type: ignore[union-attr]
        name = (acc.name if acc is not None and acc.name else None) or (None if start is None else start.name)
        shadow = start.shadow if start is not None else bool(acc is not None and acc.shadow)
        planned = None if start is None else start.planned
        if extra_kw is not None and load_id in extra_kw:
            ran_kw = _r(extra_kw[load_id])
        else:
            ran_kw = None if acc is None or acc.kw_n == 0 else _r(acc.kw_sum / acc.kw_n)
        samples = max(1, opened.samples)
        ran_what = None if acc is None else _most(acc.whats, max(1.0, WHAT_SHARE * samples))
        if planned is not None and ran_what is not None and same_action(planned.what, ran_what):
            # The battery charged as the plan said (from the grid, say): what ran takes the plan's word for it.
            ran_what = planned.what
        decisive = None if acc is None else _most(acc.reasons, max(1.0, DECISIVE_SHARE * samples))
        if decisive is not None:
            reason = decisive
        elif agrees(None if planned is None or planned.what is None else planned, ran_what, ran_kw):
            reason = None if planned is None else planned.reason
        else:
            reason = None if acc is None else _most(acc.hints, 1.0)
        params = None if acc is None or decisive is None else acc.params.get(decisive)
        if planned is not None and planned.what is None and planned.level in (None, 0):
            planned = Planned(None, planned.kw, None, planned.level)
        rows.append(LoadRow(opened.hour, load_id, kind, name, shadow, planned, ran_what, ran_kw, reason, params))
    return hour_row, rows


# ------------------------------------------------------------------------------------------------ the ledger


@dataclass(slots=True)
class Ledger:
    """The kept hours (rows by hour number) and the running one."""

    hours: dict[int, HourRow] = field(default_factory=dict)
    loads: dict[int, list[LoadRow]] = field(default_factory=dict)
    open: OpenHour | None = None

    @property
    def first_hour(self) -> int | None:
        return min(self.hours, default=None)

    def keep(self, hour_row: HourRow, rows: Iterable[LoadRow]) -> None:
        self.hours[hour_row.hour] = hour_row
        self.loads[hour_row.hour] = list(rows)

    def trim(self, today: date, zone: Any) -> bool:
        """Drop the hours before the local day `KEPT_DAYS` before `today`. Whether any went."""
        first = today - timedelta(days=KEPT_DAYS)
        cut = hour_number(datetime(first.year, first.month, first.day, tzinfo=zone))
        old = [hour for hour in self.hours if hour < cut]
        for hour in old:
            del self.hours[hour]
            self.loads.pop(hour, None)
        for hour in [hour for hour in self.loads if hour < cut]:
            del self.loads[hour]
        return bool(old)

    def since(self, start: datetime) -> Ledger:
        """A copy of the hours from `start` on, with the running one (the debug bundle's cut)."""
        first = hour_number(start)
        return Ledger(
            hours={hour: row for hour, row in self.hours.items() if hour >= first},
            loads={hour: list(rows) for hour, rows in self.loads.items() if hour >= first},
            open=self.open,
        )

    def between(self, start: datetime, end: datetime) -> list[tuple[HourRow, list[LoadRow]]]:
        first, last = hour_number(start), hour_number(end)
        return [(self.hours[hour], self.loads.get(hour, [])) for hour in sorted(self.hours) if first <= hour < last]

    # ---- the store

    def as_store(self) -> dict[str, Any]:
        """Columns (see the module): each hour's site row, each load's row with the load as an index in `loads`."""
        index: dict[tuple[str, str], int] = {}
        loads: list[list[Any]] = []
        hour_rows: list[list[Any]] = []
        load_rows: list[list[Any]] = []
        for hour in sorted(self.hours):
            row = self.hours[hour]
            hour_rows.append(
                [
                    row.hour,
                    row.samples,
                    row.planned_at_min,
                    row.price,
                    row.sell,
                    row.import_kw,
                    row.house_kw,
                    row.ceiling_kw,
                    row.ceiling,
                    row.planned_net_kw,
                ]
            )
            for load in self.loads.get(hour, ()):
                key = (load.load_id, load.kind)
                at = index.get(key)
                if at is None:
                    at = index[key] = len(loads)
                    loads.append([load.load_id, load.kind, load.name])
                else:
                    loads[at][2] = load.name or loads[at][2]
                planned = load.planned
                load_rows.append(
                    [
                        hour,
                        at,
                        1 if load.shadow else 0,
                        None if planned is None else planned.what,
                        None if planned is None else _r(planned.kw),
                        None if planned is None else planned.reason,
                        None if planned is None else planned.level,
                        load.ran_what,
                        load.ran_kw,
                        load.reason,
                        load.params,
                    ]
                )
        return {
            "schema": SCHEMA,
            "loads": loads,
            "hour_columns": list(HOUR_COLUMNS),
            "hours": hour_rows,
            "load_columns": list(LOAD_COLUMNS),
            "rows": load_rows,
            "open": None if self.open is None else self.open.as_store(),
        }

    @classmethod
    def from_store(cls, raw: Any) -> Ledger:
        """What was kept, read as untrusted: a row that does not read is left out."""
        ledger = cls()
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            return ledger
        loads = [
            item
            for item in raw.get("loads") or ()
            if isinstance(item, list) and len(item) == 3 and isinstance(item[0], str) and isinstance(item[1], str)
        ]
        for row in raw.get("hours") or ():
            if not isinstance(row, list) or len(row) != len(HOUR_COLUMNS) or not isinstance(row[0], int):
                continue
            ledger.hours[row[0]] = HourRow(
                hour=row[0],
                samples=row[1] if isinstance(row[1], int) else 0,
                planned_at_min=row[2] if isinstance(row[2], int) else None,
                price=_num_or_none(row[3]),
                sell=_num_or_none(row[4]),
                import_kw=_num_or_none(row[5]),
                house_kw=_num_or_none(row[6]),
                ceiling_kw=_num_or_none(row[7]),
                ceiling=_text_or_none(row[8]),
                planned_net_kw=_num_or_none(row[9]),
            )
        for row in raw.get("rows") or ():
            if (
                not isinstance(row, list)
                or len(row) != len(LOAD_COLUMNS)
                or not isinstance(row[0], int)
                or row[0] not in ledger.hours
                or not isinstance(row[1], int)
                or not 0 <= row[1] < len(loads)
            ):
                continue
            load_id, kind, name = loads[row[1]]
            planned = (
                None
                if row[3] is None and row[4] is None and row[5] is None and row[6] is None
                else Planned(_text_or_none(row[3]), _num_or_none(row[4]), _text_or_none(row[5]), row[6] if isinstance(row[6], int) else None)
            )
            ledger.loads.setdefault(row[0], []).append(
                LoadRow(
                    hour=row[0],
                    load_id=load_id,
                    kind=kind,
                    name=_text_or_none(name),
                    shadow=row[2] == 1,
                    planned=planned,
                    ran_what=_text_or_none(row[7]),
                    ran_kw=_num_or_none(row[8]),
                    reason=_text_or_none(row[9]),
                    params=row[10] if isinstance(row[10], dict) else None,
                )
            )
        ledger.open = OpenHour.from_store(raw.get("open"))
        return ledger


# ------------------------------------------------------------------------------------------------ the plan at the start


def snapshot(payload: Mapping[str, Any], hour: datetime) -> tuple[SiteAtStart, dict[str, LoadAtStart]] | None:
    """The plan view's lanes for the hour starting at `hour` (`site/plan_view.py:plan_view`'s answer), `None` when
    the answer holds no such hour."""
    hours = payload.get("hours") or []
    iso = [row.get("start") for row in hours]
    target = hour.astimezone(timezone.utc)
    index = next(
        (at for at, value in enumerate(iso) if isinstance(value, str) and _same(value, target)),
        None,
    )
    if index is None:
        return None
    row = hours[index]
    site = SiteAtStart(
        price=_num_or_none(row.get("price")),
        sell=_num_or_none(row.get("sell")),
        ceiling_kw=_num_or_none(row.get("ceiling_kw")),
        ceiling=_text_or_none(row.get("ceiling")),
        net_kw=_num_or_none(row.get("net_kw")),
    )
    end = target + HOUR
    loads: dict[str, LoadAtStart] = {}
    for load in payload.get("loads") or ():
        kw = load.get("kw") or []
        level = load.get("level")
        run = next(
            (
                item
                for item in load.get("runs") or ()
                if (_instant(item.get("start")) or end) < end and (_instant(item.get("end")) or target) > target
            ),
            None,
        )
        loads[str(load.get("id"))] = LoadAtStart(
            kind=str(load.get("kind")),
            name=_text_or_none(load.get("name")),
            shadow=load.get("state") == "would",
            planned=Planned(
                what=None if run is None else _text_or_none(run.get("what")),
                kw=_num_or_none(kw[index]) if index < len(kw) else None,
                reason=None if run is None else _text_or_none(run.get("reason")),
                level=level[index] if isinstance(level, list) and index < len(level) and isinstance(level[index], int) else None,
            ),
        )
    return site, loads


def _same(value: str, target: datetime) -> bool:
    moment = _instant(value)
    return moment is not None and moment.astimezone(timezone.utc) == target
