"""One charge session: from a charge start (any cause) until it stops or the car is unplugged.

A session stores no finished cost. It stores `intervals`: per price interval it drew energy in, the kWh
and the raw spot price (`costing.Slice`). `priced(fiscal)` makes the cost from them with the person's
current settings (the store does that on every read, so `cost_minor` and its siblings on a session that
a reader holds are the current figures). Money is in the market's minor unit (cent, ore), as the
planner's effective price is, and shown in the major unit like the plan's estimated cost. A figure that
could not be known stays absent: `cost_minor` is `None` for a session none of whose energy had a
published price, never zero.

A record written before that (it carries only `cost_minor`) is kept as it was, under `legacy_*` names,
and says so with `cost_basis: "stored"` until `history_import.migrate_legacy` can re-derive its intervals.

Stored JSON is untrusted: `from_dict` reads exactly the shape `as_dict` writes and answers `None` for
anything else, so one bad record never takes the others down.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, tzinfo
from typing import Any, Final

from homeassistant.util import dt as dt_util

from ..planning.planner import FiscalChoice
from .costing import price_slices, Slice

#: Where a session's energy comes from: the charger's energy register, SpotNav's own integration of a
#: smart plug's power, or an estimate from the current the charger was asked for (marked `estimated`).
SOURCE_REGISTER: Final = "register"
SOURCE_INTEGRATED: Final = "integrated_power"
SOURCE_ESTIMATED: Final = "estimated"
ENERGY_SOURCES: Final = (SOURCE_REGISTER, SOURCE_INTEGRATED, SOURCE_ESTIMATED)

#: How a session started.
STARTED_PLAN_WINDOW: Final = "plan_window"
STARTED_MANUAL: Final = "manual"
STARTED_SOLAR: Final = "solar"
STARTED_HYBRID: Final = "hybrid"
STARTED_OTHER: Final = "other"
#: An imported session: nobody recorded how it started.
STARTED_UNKNOWN: Final = "unknown"
STARTED_BY: Final = (
    STARTED_PLAN_WINDOW,
    STARTED_MANUAL,
    STARTED_SOLAR,
    STARTED_HYBRID,
    STARTED_OTHER,
    STARTED_UNKNOWN,
)

#: Where a session came from when it was not recorded live: rebuilt from the recorder's hourly
#: long-term statistics. A live session has no `source` (`None`).
SOURCE_IMPORTED_HOURLY: Final = "imported_hourly"
IMPORT_SOURCES: Final = (SOURCE_IMPORTED_HOURLY,)

#: Fields every stored record carries.
_FIELDS: Final = frozenset(
    {
        "id", "charger_id", "start", "end", "energy_kwh", "energy_source", "currency", "major_unit",
        "minor_unit", "started_by", "strategy", "vehicle_id", "vehicle_name", "solar_kwh",
        "solar_known_kwh", "last_register_kwh", "last_sample_at",
    }
)
#: Written only when set: an imported session's `source`, the market's `area_id`, and (only for a record
#: without intervals) the cost it was stored with.
_OPTIONAL_FIELDS: Final = frozenset(
    {"source", "area_id", "intervals", "legacy_priced_kwh", "legacy_cost_minor", "legacy_reference_cost_minor"}
)
#: What a record written before intervals existed carried instead.
_PRE_INTERVAL_FIELDS: Final = frozenset({"priced_kwh", "cost_minor", "reference_cost_minor"})

COST_BASIS_CURRENT: Final = "current_settings"
COST_BASIS_STORED: Final = "stored"


def _number(value: Any, *, minimum: float | None = 0.0) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return None
    return float(value)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    moment = dt_util.parse_datetime(value)
    return None if moment is None or moment.tzinfo is None else moment


@dataclass(slots=True)
class ChargeSession:
    """A session, open (`end is None`) while it accrues and closed once stored."""

    id: str
    charger_id: str
    start: datetime
    end: datetime | None
    energy_kwh: float
    energy_source: str
    #: The energy that had a published price, and what it cost (minor unit) with the settings now in
    #: force (made by `priced`); `cost_minor` is `None` until some energy was priced.
    priced_kwh: float
    cost_minor: float | None
    #: What that same priced energy would have cost at the day's average effective price.
    reference_cost_minor: float | None
    currency: str | None
    major_unit: str | None
    minor_unit: str | None
    started_by: str
    strategy: str | None
    vehicle_id: str | None
    vehicle_name: str | None
    #: Energy delivered while the solar executor knew its solar share, and the part of it that was solar.
    solar_kwh: float
    solar_known_kwh: float
    #: Open sessions only: where the energy register stood at the last sample, and when that was.
    last_register_kwh: float | None
    last_sample_at: datetime | None
    #: `None` for a session recorded live, else where an imported one came from (`IMPORT_SOURCES`).
    source: str | None = None
    #: What the session drew per price interval: the stored basis of every cost.
    intervals: tuple[Slice, ...] = ()
    #: The market the prices are from (for the fiscal settings of that market).
    area_id: str | None = None

    @property
    def cost_basis(self) -> str:
        """`stored` for a record that only has the cost it was written with, else `current_settings`."""
        if not self.intervals and self.cost_minor is not None:
            return COST_BASIS_STORED
        return COST_BASIS_CURRENT

    def priced(self, fiscal: FiscalChoice | None) -> ChargeSession:
        """This session with its cost made from its intervals with `fiscal`, or itself when it has none
        (a stored cost stays as it is)."""
        if not self.intervals:
            return self
        costing = price_slices(self.intervals, fiscal)
        if costing.priced_kwh <= 0:
            return replace(self, priced_kwh=0.0, cost_minor=None, reference_cost_minor=None)
        return replace(
            self,
            priced_kwh=costing.priced_kwh,
            cost_minor=costing.cost_minor,
            reference_cost_minor=costing.reference_cost_minor,
        )

    @property
    def imported(self) -> bool:
        return self.source is not None

    @property
    def estimated(self) -> bool:
        return self.energy_source == SOURCE_ESTIMATED

    @property
    def average_price_minor_per_kwh(self) -> float | None:
        if self.cost_minor is None or self.priced_kwh <= 0:
            return None
        return self.cost_minor / self.priced_kwh

    @property
    def solar_share(self) -> float | None:
        """The fraction of the energy that was solar, over the energy the solar executor could judge."""
        if self.solar_known_kwh <= 0:
            return None
        return min(1.0, max(0.0, self.solar_kwh / self.solar_known_kwh))

    @property
    def savings_minor(self) -> float | None:
        """Reference cost minus actual cost for the priced energy: positive when the charge was cheaper
        than the day's average price. An estimate, never an invoice."""
        if self.cost_minor is None or self.reference_cost_minor is None:
            return None
        return self.reference_cost_minor - self.cost_minor

    def as_dict(self) -> dict[str, Any]:
        """The stored shape."""
        record: dict[str, Any] = {
            "id": self.id,
            "charger_id": self.charger_id,
            "start": self.start.isoformat(),
            "end": None if self.end is None else self.end.isoformat(),
            "energy_kwh": self.energy_kwh,
            "energy_source": self.energy_source,
            "currency": self.currency,
            "major_unit": self.major_unit,
            "minor_unit": self.minor_unit,
            "started_by": self.started_by,
            "strategy": self.strategy,
            "vehicle_id": self.vehicle_id,
            "vehicle_name": self.vehicle_name,
            "solar_kwh": self.solar_kwh,
            "solar_known_kwh": self.solar_known_kwh,
            "last_register_kwh": self.last_register_kwh,
            "last_sample_at": None if self.last_sample_at is None else self.last_sample_at.isoformat(),
        }
        if self.intervals or self.cost_minor is None:
            record["intervals"] = [item.as_row() for item in self.intervals]
        else:
            record["legacy_priced_kwh"] = self.priced_kwh
            record["legacy_cost_minor"] = self.cost_minor
            record["legacy_reference_cost_minor"] = self.reference_cost_minor
        if self.source is not None:
            record["source"] = self.source
        if self.area_id is not None:
            record["area_id"] = self.area_id
        return record

    @classmethod
    def from_dict(cls, raw: Any) -> ChargeSession | None:
        """A stored record, or `None` when it is not exactly this shape."""
        if not isinstance(raw, dict) or not _FIELDS <= set(raw):
            return None
        extra = set(raw) - _FIELDS - _OPTIONAL_FIELDS - _PRE_INTERVAL_FIELDS
        if extra:
            return None
        source = raw.get("source")
        area_id = raw.get("area_id")
        if (source is not None and source not in IMPORT_SOURCES) or (
            area_id is not None and not isinstance(area_id, str)
        ):
            return None
        # The cost the record was written with: only a record that has no intervals has one.
        if "legacy_cost_minor" in raw or "legacy_priced_kwh" in raw:
            kind = "legacy_"
        elif "cost_minor" in raw:
            kind = ""
        else:
            kind = None
        intervals: list[Slice] = []
        if "intervals" in raw:
            if not isinstance(raw["intervals"], list):
                return None
            for row in raw["intervals"]:
                item = Slice.from_row(row)
                if item is None:
                    return None
                intervals.append(item)
        elif kind is None:
            return None
        start = _instant(raw["start"])
        end = None if raw["end"] is None else _instant(raw["end"])
        sample = None if raw["last_sample_at"] is None else _instant(raw["last_sample_at"])
        energy = _number(raw["energy_kwh"])
        solar = _number(raw["solar_kwh"])
        known = _number(raw["solar_known_kwh"])
        priced = 0.0 if kind is None else _number(raw.get(f"{kind}priced_kwh"))
        cost = None
        reference = None
        if kind is not None and raw.get(f"{kind}cost_minor") is not None:
            cost = _number(raw[f"{kind}cost_minor"], minimum=None)
            if cost is None:
                return None
        if kind is not None and raw.get(f"{kind}reference_cost_minor") is not None:
            reference = _number(raw[f"{kind}reference_cost_minor"], minimum=None)
            if reference is None:
                return None
        register = None if raw["last_register_kwh"] is None else _number(raw["last_register_kwh"])
        if (
            start is None
            or (raw["end"] is not None and end is None)
            or (raw["last_sample_at"] is not None and sample is None)
            or None in (energy, priced, solar, known)
            or (raw["last_register_kwh"] is not None and register is None)
            or raw["energy_source"] not in ENERGY_SOURCES
            or raw["started_by"] not in STARTED_BY
            or _text(raw["id"]) is None
            or _text(raw["charger_id"]) is None
        ):
            return None
        for key in ("currency", "major_unit", "minor_unit", "strategy", "vehicle_id", "vehicle_name"):
            if raw[key] is not None and not isinstance(raw[key], str):
                return None
        return cls(
            id=raw["id"],
            charger_id=raw["charger_id"],
            start=start,
            end=end,
            energy_kwh=energy,  # type: ignore[arg-type]
            energy_source=raw["energy_source"],
            priced_kwh=priced,  # type: ignore[arg-type]
            cost_minor=cost,
            reference_cost_minor=reference,
            currency=raw["currency"],
            major_unit=raw["major_unit"],
            minor_unit=raw["minor_unit"],
            started_by=raw["started_by"],
            strategy=raw["strategy"],
            vehicle_id=raw["vehicle_id"],
            vehicle_name=raw["vehicle_name"],
            solar_kwh=solar,  # type: ignore[arg-type]
            solar_known_kwh=known,  # type: ignore[arg-type]
            last_register_kwh=register,
            last_sample_at=sample,
            source=source,
            intervals=tuple(intervals),
            area_id=area_id,
        )

    def public(self, zone: tzinfo | None = None) -> dict[str, Any]:
        """The wire shape: major-unit cost, minor-unit prices, instants as offset-bearing ISO strings in
        `zone` (Home Assistant's own, so a client can read the local wall time off the string)."""
        cost = self.cost_minor
        start = self.start if zone is None else self.start.astimezone(zone)
        end = self.end if zone is None or self.end is None else self.end.astimezone(zone)
        savings = self.savings_minor
        reference = self.reference_cost_minor
        average = self.average_price_minor_per_kwh
        return {
            "id": self.id,
            "start": start.isoformat(),
            "end": None if end is None else end.isoformat(),
            "energy_kwh": round(self.energy_kwh, 3),
            "energy_source": self.energy_source,
            "estimated": self.estimated,
            "cost": None if cost is None else round(cost / 100, 4),
            "currency": self.currency,
            "major_unit": self.major_unit,
            "minor_unit": self.minor_unit,
            "average_price_minor_per_kwh": None if average is None else round(average, 3),
            "started_by": self.started_by,
            "source": self.source,
            "cost_basis": self.cost_basis,
            "strategy": self.strategy,
            "vehicle": self.vehicle_name,
            "solar_share": None if self.solar_share is None else round(self.solar_share, 3),
            "reference_cost": None if reference is None else round(reference / 100, 4),
            "savings": None if savings is None else round(savings / 100, 4),
            "savings_estimate": True,
        }
