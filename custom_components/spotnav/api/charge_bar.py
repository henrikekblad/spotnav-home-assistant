"""The dashboard's `progress` block: how far a running charge has come and when it ends.

Decided once here, so the card and the app draw the same bar from the same numbers (the app's
`ChargeBar.kt` was the reference for the rules and stays their twin). Pure: `ProgressFacts` is one
captured instant (`dashboard.progress_facts` builds it) and nothing is read or guessed here; a number
that is missing makes its field `null`.

- A bar only while the charge is on (`live.charging`), not while the integration starts up, and not
  for a charger that reports no car, a finished car or an error.
- It moves while current flows: the charger says `charging` (or nothing), the car is not seen taking
  no current, and a measured current or power (where the charger has one) is not near zero.
  Otherwise it stands still and states no end and no power.
- The plan drives the charge when an installed window holds now and no pause stands. Any other
  running charge (a person's Start, a charge under a scheduled pause, one the charger began itself, a
  sun charge outside the windows) counts to the car's own limit (`vehicle_limit`): level / limit (else
  100 %), the end from the battery's room; with no level, the `open` bar and its power. A sun charge
  states no end unless a person started it.
- While the plan drives, the driver counts: a target (level / target, the end from `soc.need_kwh`),
  Fill (level / the car's limit, the end from the room), a fixed amount (`plan.delivered_kwh` / the
  whole amount; no bar without `delivered_kwh`). The end is laid along the remaining windows and never
  falls after the last one; solar and hybrid state no end.
- The percent is rounded down and kept within 0-100.
- The power: measured first (the charger's power sensor, else its measured current over the phases the
  charge uses), else what SpotNav assigned now, else the installed schedule's power or current, else
  the proposal's power, else the settings' current.
- From the open session: when this charge began, the car's level then, and the energy measured since
  (`null` for an estimated session).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from ..planning.planner import power_kw as planned_power_kw
from ..util import aware_iso, finite_number
from ..vehicles.soc_estimate import CHARGE_EFFICIENCY

BASIS_TARGET: Final = "target"
BASIS_ENERGY: Final = "energy"
BASIS_VEHICLE_LIMIT: Final = "vehicle_limit"
BASIS_OPEN: Final = "open"

POWER_MEASURED: Final = "measured"
POWER_PLANNED: Final = "planned"

#: Connection states with no bar: no car, a finished car, an error.
NO_BAR_CONNECTIONS: Final = frozenset({"disconnected", "finished", "error"})
#: Connection states in which current flows (`unknown`: the charger does not say).
FLOWING_CONNECTIONS: Final = frozenset({"charging", "unknown"})
#: A measured current below this, or a measured power below `IDLE_POWER_KW`, is no charge.
IDLE_CURRENT_A: Final = 0.5
IDLE_POWER_KW: Final = 0.1

_SUN_STRATEGIES: Final = frozenset({"solar", "hybrid"})
_DRIVER_TARGET: Final = "target_soc"

Span = tuple[datetime, datetime]


@dataclass(frozen=True, slots=True)
class ProgressFacts:
    """One captured instant, everything the bar is decided from."""

    now: datetime
    charging: bool
    starting_up: bool = False
    #: The `connection` block's state.
    connection: str = "unknown"
    #: The vehicle-side observation says the car asks for no current.
    vehicle_not_requesting: bool = False
    strategy: str | None = None
    #: Automatic execution is paused (any pause, a person's Start or Stop included).
    paused: bool = False
    #: The pause is a person's Start: Home Assistant charges until the car is full or unplugged.
    person_started: bool = False
    installed_periods: tuple[Span, ...] = ()
    #: The settings' driver (`target_soc`, `manual_kwh`); `None` with no settings record.
    driver: str | None = None
    fill_to_limit: bool = False
    soc_percent: float | None = None
    target_percent: float | None = None
    vehicle_max_percent: float | None = None
    need_kwh: float | None = None
    room_kwh: float | None = None
    capacity_kwh: float | None = None
    efficiency: float | None = CHARGE_EFFICIENCY
    #: A manual need's count (`plan.delivered_kwh`, `plan.remaining_kwh`) and the amount asked for.
    plan_delivered_kwh: float | None = None
    plan_remaining_kwh: float | None = None
    requested_kwh: float | None = None
    #: The charger's own measurements now.
    measured_power_kw: float | None = None
    measured_current_a: float | None = None
    #: The phases the charge uses (the installed schedule's, else the effective count).
    phases: int | None = None
    voltage_between_phases_v: float = 400.0
    #: The current SpotNav asks of the charger now (a plan, solar or the site's limit).
    assigned_current_a: float | None = None
    installed_power_kw: float | None = None
    installed_amps: float | None = None
    installed_phases: int | None = None
    proposal_power_kw: float | None = None
    settings_amps: float | None = None
    #: The open charge session: when it began, the car's level then, the energy measured since.
    session_started_at: datetime | None = None
    session_start_soc_percent: float | None = None
    session_delivered_kwh: float | None = None


def percent_of(fraction: float) -> int | None:
    """A share as a whole percent, rounded down and kept within 0-100; `None` for no number."""
    if not math.isfinite(fraction):
        return None
    return max(0, min(100, math.floor(fraction * 100.0 + 1e-9)))


def plan_drives(facts: ProgressFacts) -> bool:
    """Whether the plan drives the charge now: an installed window holds now and no pause stands."""
    if facts.paused:
        return False
    return any(start <= facts.now < end for start, end in facts.installed_periods)


def charge_bar(facts: ProgressFacts) -> dict[str, Any] | None:
    """The `progress` block, or `None` when no bar is shown."""
    if not facts.charging or facts.starting_up or facts.connection in NO_BAR_CONNECTIONS:
        return None
    moving = _moving(facts)
    power, source = _power(facts) if moving else (None, None)
    follows = facts.strategy in _SUN_STRATEGIES
    drives = plan_drives(facts)
    if facts.person_started or not drives:
        shown = _to_own_limit(facts)
        end_known = facts.person_started or not follows
    else:
        shown = _driven(facts)
        if shown is None:
            return None
        end_known = not follows
    basis, fraction, remaining = shown
    ends_at = None
    if moving and end_known and basis != BASIS_OPEN:
        ends_at = _ends_at(facts, remaining, power, along_windows=drives and not facts.person_started)
    start_soc = finite_number(facts.session_start_soc_percent)
    delivered = finite_number(facts.session_delivered_kwh)
    return {
        "basis": basis,
        "percent": None if fraction is None else percent_of(fraction),
        "ends_at": None if ends_at is None else aware_iso(ends_at.astimezone(timezone.utc)),
        "power_kw": None if power is None else round(power, 2),
        "power_source": source,
        "moving": moving,
        "start_soc_percent": None if start_soc is None else round(start_soc, 1),
        "started_at": aware_iso(facts.session_started_at),
        "delivered_kwh": None if delivered is None else round(delivered, 2),
    }


def _moving(facts: ProgressFacts) -> bool:
    if facts.connection not in FLOWING_CONNECTIONS or facts.vehicle_not_requesting:
        return False
    power = finite_number(facts.measured_power_kw)
    if power is not None:
        return power >= IDLE_POWER_KW
    current = finite_number(facts.measured_current_a)
    return current is None or current >= IDLE_CURRENT_A


def _ceiling(facts: ProgressFacts) -> float:
    """The car's own charge limit, else 100 %."""
    limit = finite_number(facts.vehicle_max_percent)
    return 100.0 if limit is None or limit <= 0 else min(limit, 100.0)


def _to_own_limit(facts: ProgressFacts) -> tuple[str, float | None, float | None]:
    """A charge to the car's own limit; with no level, the open bar."""
    level = finite_number(facts.soc_percent)
    if level is None:
        return BASIS_OPEN, None, None
    ceiling = _ceiling(facts)
    room = finite_number(facts.room_kwh)
    if room is None:
        capacity = finite_number(facts.capacity_kwh)
        efficiency = finite_number(facts.efficiency)
        if capacity is not None and capacity > 0 and efficiency is not None and efficiency > 0:
            room = max(0.0, capacity * (ceiling - level) / 100.0 / efficiency)
    return BASIS_VEHICLE_LIMIT, level / ceiling, room


def _driven(facts: ProgressFacts) -> tuple[str, float | None, float | None] | None:
    """A charge the plan drives, by its driver; `None` when the driver's numbers are missing."""
    if facts.driver is None:
        return None
    level = finite_number(facts.soc_percent)
    if facts.driver == _DRIVER_TARGET:
        stated = finite_number(facts.target_percent)
        if level is None or stated is None:
            return None
        # Rounded as the plan rounds it (half to even), never above the car's own limit.
        goal = min(float(round(stated)), _ceiling(facts))
        if goal <= 0:
            return None
        return BASIS_TARGET, level / goal, finite_number(facts.need_kwh)
    if facts.fill_to_limit:
        if level is None:
            return None
        return BASIS_VEHICLE_LIMIT, level / _ceiling(facts), finite_number(facts.room_kwh)
    delivered = finite_number(facts.plan_delivered_kwh)
    if delivered is None:
        return None
    left = finite_number(facts.plan_remaining_kwh)
    total = delivered + left if left is not None else finite_number(facts.requested_kwh)
    if total is None or total <= 0:
        return None
    return BASIS_ENERGY, delivered / total, left if left is not None else max(0.0, total - delivered)


def _kw(amps: float | None, phases: int | None, voltage: float) -> float | None:
    if amps is None or amps <= 0 or phases not in (1, 3):
        return None
    return planned_power_kw(amps, phases, voltage)  # type: ignore[arg-type]


def _power(facts: ProgressFacts) -> tuple[float | None, str | None]:
    """The power the charge runs at in kW and where it came from, or `(None, None)`."""
    voltage = facts.voltage_between_phases_v
    phases = facts.installed_phases if facts.installed_phases in (1, 3) else facts.phases
    measured = finite_number(facts.measured_power_kw)
    if measured is None or measured <= 0:
        measured = _kw(finite_number(facts.measured_current_a), phases, voltage)
    if measured is not None:
        return measured, POWER_MEASURED
    for candidate in (
        _kw(finite_number(facts.assigned_current_a), phases, voltage),
        finite_number(facts.installed_power_kw),
        _kw(finite_number(facts.installed_amps), facts.installed_phases, voltage),
        finite_number(facts.proposal_power_kw),
        _kw(finite_number(facts.settings_amps), phases, voltage),
    ):
        if candidate is not None and candidate > 0:
            return candidate, POWER_PLANNED
    return None, None


def _ends_at(
    facts: ProgressFacts, remaining: float | None, power: float | None, *, along_windows: bool
) -> datetime | None:
    """When `remaining` kWh will have been charged, or `None` when it cannot be said."""
    energy = finite_number(remaining)
    if energy is None or energy <= 0 or power is None or power <= 0:
        return None
    hours = energy / power
    now = facts.now
    if not along_windows:
        return _second(now + timedelta(hours=hours))
    windows = sorted((span for span in facts.installed_periods if span[1] > span[0]), key=lambda span: span[0])
    for start, end in windows:
        if end <= now:
            continue
        begin = max(start, now)
        length = (end - begin).total_seconds() / 3600.0
        if hours <= length:
            return _second(begin + timedelta(hours=hours))
        hours -= length
    return windows[-1][1] if windows else _second(now + timedelta(hours=hours))


def _second(moment: datetime) -> datetime:
    """To the nearest whole second: an end is never more exact than that."""
    return (moment + timedelta(microseconds=500_000)).replace(microsecond=0)
