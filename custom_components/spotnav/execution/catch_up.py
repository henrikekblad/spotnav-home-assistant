"""Charge on after a missed departure: the catch-up.

Pure: no Home Assistant imports. When a departure passes with the car short of what the plan was for (its target, by
the rule that ends a plan as reached, or its requested energy), still plugged in, and with no person's pause or Stop in
force, the plan charges on at once, whatever the price, until the first of: the need is met, the car is unplugged, a
person's Stop or pause, `LIMIT` after the departure, or the car ends the charge itself. Without this, the plan would
move on to the next departure and leave the car idle at the moment its owner wanted it full.

The catch-up is the plan's own charge (owner `plan`): one period from the departure to its end, installed as any plan
is, so load balancing, the fuse, the import target, a safety stop's hold and resume, a target's stop and the car's own
end all apply as to any window. Its record (`CatchUp`) is saved with the charger's Auto record, so a restart goes on to
the same end and never starts or extends one: only the departure's own appointment starts it.

The planner (`planning/auto_controller.py`) owns the facts, the record and the appointments; this module owns the
record, its rules and the plan it runs, so they can be read, and tested, on their own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from ..planning.planner import (
    CONTRACT_VERSION,
    effective_minor_per_kwh,
    FiscalChoice,
    PlanResult,
    PlannerInputError,
    planning_slots,
    SelectedSlot,
    SLOT_HOURS,
    STEP_MINUTES,
)
from ..pricing.relay_contract import PriceDocument

#: How long after the departure a catch-up may run.
CATCH_UP_LIMIT_S: Final = 3 * 3600
LIMIT: Final = timedelta(seconds=CATCH_UP_LIMIT_S)

#: A catch-up that ran to its end with the need still not met (`CatchUp.ended`): the status says so until the unplug.
ENDED_SHORT: Final = "short"

#: Less than this much energy left is a met need (as `price_wait.EPSILON_KWH`).
EPSILON_KWH: Final = 1e-6


@dataclass(frozen=True, slots=True)
class CatchUp:
    """One catch-up after a missed departure, as saved.

    `departure` is the departure that was missed and `until` the latest it may run to (`LIMIT` after it); `end` is the
    end of the period it charges in: `until`, or for an energy need counted with nothing that can end it at the need,
    where the need is expected to be delivered at the planned power (never past `until`). `target_percent` and
    `level_percent` are the target and the car's level when it started (`None` for an energy need), `requested_kwh`
    and `short_kwh` the energy need of that departure and what was still missing then. `ended` is `ENDED_SHORT` once
    it ran to its end short of the need; `level_percent` and `delivered_kwh` then say where it ended.
    """

    departure: datetime
    until: datetime
    end: datetime
    requested_kwh: float
    short_kwh: float
    target_percent: float | None = None
    level_percent: float | None = None
    #: An energy need's energy delivered toward the missed departure when it ended short (`None` while it runs).
    delivered_kwh: float | None = None
    ended: str | None = None

    @property
    def energy_need(self) -> bool:
        """Whether the need is an amount of energy rather than a target level."""
        return self.target_percent is None

    def running(self, now: datetime) -> bool:
        """Whether this catch-up charges now: not ended, and its period not over."""
        return self.ended is None and self.departure <= now < self.end

    def as_dict(self) -> dict[str, Any]:
        return {
            "departure": self.departure.isoformat(),
            "until": self.until.isoformat(),
            "end": self.end.isoformat(),
            "requested_kwh": round(self.requested_kwh, 6),
            "short_kwh": round(self.short_kwh, 6),
            "target_percent": self.target_percent,
            "level_percent": self.level_percent,
            "delivered_kwh": None if self.delivered_kwh is None else round(self.delivered_kwh, 6),
            "ended": self.ended,
        }

    @classmethod
    def from_stored(cls, raw: Any) -> CatchUp | None:
        """A saved record, or `None` when it cannot be trusted as one (a catch-up is never repaired, only dropped)."""
        if not isinstance(raw, dict):
            return None
        departure = _instant(raw.get("departure"))
        until = _instant(raw.get("until"))
        end = _instant(raw.get("end"))
        requested = _number(raw.get("requested_kwh"))
        short = _number(raw.get("short_kwh"))
        if departure is None or until is None or end is None or requested is None or short is None:
            return None
        if not departure < end <= until or until > departure + LIMIT or requested < 0 or short < 0:
            return None
        ended = raw.get("ended")
        if ended not in (None, ENDED_SHORT):
            return None
        return cls(
            departure=departure,
            until=until,
            end=end,
            requested_kwh=requested,
            short_kwh=short,
            target_percent=_number(raw.get("target_percent")),
            level_percent=_number(raw.get("level_percent")),
            delivered_kwh=_number(raw.get("delivered_kwh")),
            ended=ended,
        )

    def ended_short(self, *, level_percent: float | None, delivered_kwh: float | None) -> CatchUp:
        """This catch-up ended at its end with the need not met, at that level or energy."""
        return replace(self, ended=ENDED_SHORT, level_percent=level_percent, delivered_kwh=delivered_kwh)


def starts(
    *,
    planned: bool,
    met: bool | None,
    plugged_in: bool | None,
    paused: bool,
    person_charging: bool,
) -> bool:
    """Whether a departure that just passed starts a catch-up. Pure.

    `planned`: the plan in force was for this departure and had a need for it. `met`: the need is met by the rule that
    ends a plan as reached (`None`: nothing can tell, which never starts one). `plugged_in`: the charger says a car is
    plugged in (`None`: it cannot tell, which never starts one). `paused`: Auto is paused, a person's Stop or Start
    among them. `person_charging`: a charge a person started (Charge now) runs; it goes on past the departure by itself.
    """
    return planned and met is False and plugged_in is True and not paused and not person_charging


def energy_end(departure: datetime, until: datetime, short_kwh: float, power_kw: float) -> datetime:
    """Where an energy need is expected to be delivered at `power_kw` from the departure, rounded up to the whole minute
    and never past `until` (the whole span when the power is not known)."""
    if not math.isfinite(power_kw) or power_kw <= 0 or not math.isfinite(short_kwh):
        return until
    minutes = math.ceil(short_kwh / power_kw * 60)
    return min(until, departure + timedelta(minutes=max(1, minutes)))


def plan_result(
    catch_up: CatchUp,
    *,
    now: datetime,
    documents: tuple[PriceDocument, ...],
    area_id: str,
    tz: str,
    currency: str,
    major_unit: str,
    minor_unit: str,
    fiscal: FiscalChoice,
    power_kw: float,
    consumption_kwh_per_10km: float,
) -> PlanResult:
    """The plan a running catch-up installs: one period from the departure to its end, at the charge's full power.

    Its slots are the quarter-hours the period touches, priced where prices are published (`unpriced` when any is
    not), so the plan and its cost read as any plan's. Its energy is the need still missing at the departure, at most
    what the period holds. Built from the saved record alone, so every calculation while it runs makes the same plan.
    """
    start, end = catch_up.departure, catch_up.end
    try:
        published = {slot.start: slot for slot in planning_slots(documents, currency=currency, timezone=tz)}
    except PlannerInputError:
        published = {}
    step = timedelta(minutes=STEP_MINUTES)
    utc_start = start.astimezone(timezone.utc)
    first = utc_start - timedelta(
        minutes=utc_start.minute % STEP_MINUTES, seconds=utc_start.second, microseconds=utc_start.microsecond
    )
    slots: list[SelectedSlot] = []
    cost_minor = 0.0
    unpriced = False
    held_kwh = power_kw * (end - start).total_seconds() / 3600 if power_kw > 0 else 0.0
    planned_kwh = min(catch_up.short_kwh, held_kwh) if held_kwh > 0 else catch_up.short_kwh
    cursor = first
    while cursor < end:
        found = published.get(cursor)
        overlap_h = (min(cursor + step, end) - max(cursor, start)).total_seconds() / 3600
        if found is None:
            unpriced = True
            slots.append(
                SelectedSlot(
                    start=cursor, end=cursor + step, local_major_per_kwh=0.0, effective_minor_per_kwh=0.0,
                    unpriced=True, source="unpublished",
                )
            )
        else:
            effective = effective_minor_per_kwh(found.local_major_per_kwh, fiscal, cursor)
            slots.append(
                SelectedSlot(
                    start=cursor, end=cursor + step, local_major_per_kwh=found.local_major_per_kwh,
                    effective_minor_per_kwh=effective, unpriced=False, source=found.source,
                )
            )
            cost_minor += overlap_h * power_kw * effective
        cursor += step
    # The cost of what the period holds, scaled to the energy it is for.
    cost = 0.0 if held_kwh <= 0 else cost_minor / 100 * planned_kwh / held_kwh
    priced = sum(1 for slot in slots if not slot.unpriced)
    return PlanResult(
        version=CONTRACT_VERSION,
        area_id=area_id,
        timezone=tz,
        currency=currency,
        major_unit=major_unit,
        minor_unit=minor_unit,
        now=now,
        reason=None,
        power_kw=power_kw,
        requested_kwh=catch_up.short_kwh,
        delivered_kwh=planned_kwh,
        distance_mil=planned_kwh / consumption_kwh_per_10km if consumption_kwh_per_10km > 0 else 0.0,
        estimated_cost=cost,
        periods=((start, end),),
        slots=tuple(slots),
        unpriced_slots=len(slots) - priced,
        priced_slots=priced,
        unpriced=unpriced,
        slots_needed=max(1, math.ceil(planned_kwh / (power_kw * SLOT_HOURS))) if power_kw > 0 else len(slots),
        now_cost=cost,
    )


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
