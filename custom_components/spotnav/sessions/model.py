"""One charge session: from a charge start (any cause) until it stops or the car is unplugged.

Money is kept in the market's minor unit (cent, ore), exactly as the planner's effective price is, and
shown in the major unit like the plan's estimated cost. A figure that could not be known stays absent:
`cost_minor` is `None` for a session none of whose energy had a published price, never zero.

Stored JSON is untrusted: `from_dict` reads exactly the shape `as_dict` writes and answers `None` for
anything else, so one bad record never takes the others down.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from homeassistant.util import dt as dt_util

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
STARTED_BY: Final = (
    STARTED_PLAN_WINDOW,
    STARTED_MANUAL,
    STARTED_SOLAR,
    STARTED_HYBRID,
    STARTED_OTHER,
)

#: Fields a stored record carries, all of them, always.
_FIELDS: Final = frozenset(
    {
        "id", "charger_id", "start", "end", "energy_kwh", "energy_source", "priced_kwh", "cost_minor",
        "reference_cost_minor", "currency", "major_unit", "minor_unit", "started_by", "strategy",
        "vehicle_id", "vehicle_name", "solar_kwh", "solar_known_kwh", "last_register_kwh",
        "last_sample_at",
    }
)


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
    #: The energy that had a published price, and what it cost (minor unit); `cost_minor` is `None`
    #: until some energy was priced.
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
        return {
            "id": self.id,
            "charger_id": self.charger_id,
            "start": self.start.isoformat(),
            "end": None if self.end is None else self.end.isoformat(),
            "energy_kwh": self.energy_kwh,
            "energy_source": self.energy_source,
            "priced_kwh": self.priced_kwh,
            "cost_minor": self.cost_minor,
            "reference_cost_minor": self.reference_cost_minor,
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

    @classmethod
    def from_dict(cls, raw: Any) -> ChargeSession | None:
        """A stored record, or `None` when it is not exactly this shape."""
        if not isinstance(raw, dict) or set(raw) != _FIELDS:
            return None
        start = _instant(raw["start"])
        end = None if raw["end"] is None else _instant(raw["end"])
        sample = None if raw["last_sample_at"] is None else _instant(raw["last_sample_at"])
        energy = _number(raw["energy_kwh"])
        priced = _number(raw["priced_kwh"])
        solar = _number(raw["solar_kwh"])
        known = _number(raw["solar_known_kwh"])
        cost = None if raw["cost_minor"] is None else _number(raw["cost_minor"], minimum=None)
        reference = (
            None if raw["reference_cost_minor"] is None else _number(raw["reference_cost_minor"], minimum=None)
        )
        register = None if raw["last_register_kwh"] is None else _number(raw["last_register_kwh"])
        if (
            start is None
            or (raw["end"] is not None and end is None)
            or (raw["last_sample_at"] is not None and sample is None)
            or None in (energy, priced, solar, known)
            or (raw["cost_minor"] is not None and cost is None)
            or (raw["reference_cost_minor"] is not None and reference is None)
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
        )

    def public(self) -> dict[str, Any]:
        """The wire shape: major-unit cost, minor-unit prices, instants as offset-bearing ISO strings."""
        cost = self.cost_minor
        savings = self.savings_minor
        reference = self.reference_cost_minor
        average = self.average_price_minor_per_kwh
        return {
            "id": self.id,
            "start": self.start.isoformat(),
            "end": None if self.end is None else self.end.isoformat(),
            "energy_kwh": round(self.energy_kwh, 3),
            "energy_source": self.energy_source,
            "estimated": self.estimated,
            "cost": None if cost is None else round(cost / 100, 4),
            "currency": self.currency,
            "major_unit": self.major_unit,
            "minor_unit": self.minor_unit,
            "average_price_minor_per_kwh": None if average is None else round(average, 3),
            "started_by": self.started_by,
            "strategy": self.strategy,
            "vehicle": self.vehicle_name,
            "solar_share": None if self.solar_share is None else round(self.solar_share, 3),
            "reference_cost": None if reference is None else round(reference / 100, 4),
            "savings": None if savings is None else round(savings / 100, 4),
            "savings_estimate": True,
        }
