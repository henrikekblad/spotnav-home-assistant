"""SpotNav setting a heating zone's thermostat, pure (plan `plans/v3_zones_water_heater.md` section 2, the owner's
decisions of 2026-11-20): which thermostats qualify, the setpoint each plan hour asks for, the per-zone comfort and the
run rules. The shell that reads and writes is `heat/zones_control.py`.

**Relative to the person's own value** (D1): SpotNav keeps each zone's `home_c`, the setpoint it reads while it holds
nothing, and every write is relative to it. The zone's offset in the settings no longer acts. By the heat plan's
action for the hour (`heat/shadow_rule.band`, the first plan, not the hot water's level-A one):

| action | setpoint |
| --- | --- |
| `normal` | nothing (the person's value stands) |
| `lower` (an expensive or tight hour) | `home_c - LOWER_C` (D2: 1 °C) |
| `preheat`, `return` | `home_c + RAISE_C` |
| `night` (the cooler night) | `home_c - night.delta_c` |
| `away` | `home_c - away_delta_c` |

Every value is rounded to the thermostat's `target_temp_step` (else 0.5) and kept within its `min_temp`/`max_temp`
(`fit`). One degree is deeper than the 0.5 °C band so the element really stops, and shallow enough that the thermostat
itself holds the zone within a degree if Home Assistant dies mid-lowering.

**Per-zone comfort** on the zone's own `current_temperature` (no setting): a lowering is released at once when the zone
reads below `home_c - COMFORT_C` and none starts there (`zone_cold`); a pre-heat (and the return's warm-up) ends when
the zone reads at or above `home_c + COMFORT_C`, does not start there, and does not start again in the same hour once
ended (`zone_warm`). The night and away are what the household asked for: not released, only kept within the limits.

**The run rules** of a lowering are the heat pump relay's (`relay_control.decide`, per zone): at least 45 minutes, at
most three hours in a row, an hour between, six hours in a rolling day, never at -15 °C or colder outdoors; with
`MAX_WRITES_DAY` in place of the relay's write cap. Nothing at all while the plan or the outdoor reading is stale.

**Qualifies** (`qualifies`, looked at every time, the reason in the diagnostics only): a climate entity with one
setpoint (`TARGET_TEMPERATURE`; a range-only one does not), in `heat` mode (not `auto`: a thermostat on its own
schedule overwrites the setpoint; not `cool`, `heat_cool` or `off`: an air-to-air pump in summer is left alone), with a
numeric `current_temperature` and setpoint, available. SpotNav never writes the mode, the fan or a preset.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from ..core.parse import finite_number
from .config import BAND_C
from .relay_control import SAFETY_SETTLING, SAFETY_STALE, Situation, decide as relay_decide
from .shadow_rule import ACTION_AWAY, ACTION_LOWER, ACTION_NIGHT, ACTION_NORMAL, ACTION_PREHEAT, ACTION_RETURN

#: How far a lowering sets the zone below the person's value (°C, decision D2).
LOWER_C: Final = 1.0
#: How far a pre-heat (and the return's warm-up) sets it above.
RAISE_C: Final = BAND_C
#: The zone's own comfort band around the person's value.
COMFORT_C: Final = BAND_C
#: A thermostat without a step of its own.
DEFAULT_STEP_C: Final = 0.5
#: Setpoint writes per zone per local day, the night and away included; at the cap the person's value is written back.
MAX_WRITES_DAY: Final = 24
#: How long the zones' lowering keeps the energy it saves: the same kWh come back over this long after it ends.
REBOUND: Final = timedelta(hours=2)

# Home Assistant's `ClimateEntityFeature` bits, kept here so the rules stay pure.
FEATURE_TARGET_TEMPERATURE: Final = 1
FEATURE_TARGET_TEMPERATURE_RANGE: Final = 2

#: Why a zone does not qualify (diagnostics only).
WHY_UNAVAILABLE: Final = "unavailable"
WHY_RANGE_ONLY: Final = "range_only"
WHY_NO_SETPOINT: Final = "no_setpoint"
WHY_NOT_HEAT: Final = "not_heat_mode"
WHY_NO_TEMPERATURE: Final = "no_temperature"

REASON_ZONE_COLD: Final = "zone_cold"
REASON_ZONE_WARM: Final = "zone_warm"
REASON_NIGHT: Final = "night"
REASON_AWAY: Final = "away"
REASON_RETURN: Final = "return"
REASON_PREHEAT: Final = "preheat"

#: The actions that write a setpoint above the person's value, and the household's own (never released for comfort).
RAISES: Final = frozenset({ACTION_PREHEAT, ACTION_RETURN})
HOUSEHOLD: Final = frozenset({ACTION_NIGHT, ACTION_AWAY})


def qualifies(state: str | None, attributes: Mapping[str, Any]) -> str | None:
    """`None` when the thermostat qualifies, else why not (see the module)."""
    if state is None or state in ("unavailable", "unknown"):
        return WHY_UNAVAILABLE
    features = attributes.get("supported_features")
    features = int(features) if isinstance(features, (int, float)) and not isinstance(features, bool) else 0
    if not features & FEATURE_TARGET_TEMPERATURE:
        return WHY_RANGE_ONLY if features & FEATURE_TARGET_TEMPERATURE_RANGE else WHY_NO_SETPOINT
    if state != "heat":
        return WHY_NOT_HEAT
    if finite_number(attributes.get("temperature")) is None:
        return WHY_NO_SETPOINT
    if finite_number(attributes.get("current_temperature")) is None:
        return WHY_NO_TEMPERATURE
    return None


@dataclass(frozen=True, slots=True)
class Limits:
    """A thermostat's own step and range."""

    step: float = DEFAULT_STEP_C
    low: float | None = None
    high: float | None = None

    @classmethod
    def of(cls, attributes: Mapping[str, Any]) -> Limits:
        step = finite_number(attributes.get("target_temp_step"))
        return cls(
            step if step is not None and step > 0 else DEFAULT_STEP_C,
            finite_number(attributes.get("min_temp")),
            finite_number(attributes.get("max_temp")),
        )


def fit(value: float, limits: Limits) -> float:
    """`value` on the thermostat's step and within its range."""
    stepped = round(round(value / limits.step) * limits.step, 3)
    if limits.low is not None:
        stepped = max(stepped, limits.low)
    if limits.high is not None:
        stepped = min(stepped, limits.high)
    return stepped


def same(read: float | None, written: float | None, limits: Limits) -> bool:
    """Whether the setpoint read is the one written (less than half a step apart)."""
    if read is None or written is None:
        return False
    return abs(read - written) < limits.step / 2 + 1e-6


def read_back(read: float | None, written: float, limits: Limits) -> bool:
    """Whether a write shows on the thermostat: within one step of the value written."""
    return read is not None and abs(read - written) <= limits.step + 1e-6


def taken(read: float | None, written: float, before: float | None, limits: Limits) -> bool:
    """Whether a write has shown on the thermostat: within one step of the value written (`read_back`), and no longer
    the value it showed before the write. Where the two are a step or less apart (a pre-heat of half a degree on a
    half-degree thermostat, a lowering of one on a whole-degree one) the old value still showing is no read-back: a
    lost write must never pass for one that took, least of all the person's own value written back."""
    if not read_back(read, written, limits):
        return False
    return before is None or same(read, written, limits) or not same(read, before, limits)


def aim(action: str, home_c: float, limits: Limits, *, night_delta_c: float, away_delta_c: float) -> float | None:
    """The setpoint an action asks for (see the table), `None` for the person's own value."""
    if action == ACTION_LOWER:
        return fit(home_c - LOWER_C, limits)
    if action in RAISES:
        return fit(home_c + RAISE_C, limits)
    if action == ACTION_NIGHT:
        return fit(home_c - night_delta_c, limits)
    if action == ACTION_AWAY:
        return fit(home_c - away_delta_c, limits)
    return None


@dataclass(frozen=True, slots=True)
class ZoneSituation:
    now: datetime
    #: The plan's action for the running hour (`None` without a plan).
    action: str | None
    #: The end of the plan's lowered run that holds now (`relay_control.planned_run_end`).
    plan_until: datetime | None
    #: Until when the plan's running action lasts (shown).
    action_until: datetime | None
    home_c: float
    current_c: float | None
    #: What SpotNav holds on the zone now (an action, `None` for nothing) and since when.
    held: str | None
    held_since: datetime | None
    #: When SpotNav's last lowering of the zone ended, and its lowered time over the last 24 hours.
    last_end: datetime | None
    lowered_24h: timedelta
    writes_today: int
    #: When the last pre-heat ended because the zone was warm enough.
    raise_ended: datetime | None = None
    #: `relay_control`'s safety reasons that apply to a zone: `stale`, `too_cold`, `settling`.
    safety: str | None = None
    night_delta_c: float = 1.0
    away_delta_c: float = 3.0
    limits: Limits = Limits()


@dataclass(frozen=True, slots=True)
class ZoneDecision:
    action: str
    #: The setpoint to hold; `None`: the person's own value.
    setpoint_c: float | None
    reason: str
    until: datetime | None = None


def decide(s: ZoneSituation) -> ZoneDecision:
    """What the zone's thermostat is to be set to now (see the module)."""
    if s.action is None or s.safety == SAFETY_STALE:
        return ZoneDecision(ACTION_NORMAL, None, SAFETY_STALE)
    action = s.action
    if action in HOUSEHOLD:
        setpoint = aim(action, s.home_c, s.limits, night_delta_c=s.night_delta_c, away_delta_c=s.away_delta_c)
        return ZoneDecision(action, setpoint, REASON_NIGHT if action == ACTION_NIGHT else REASON_AWAY, s.action_until)
    if action in RAISES:
        hour = s.now.replace(minute=0, second=0, microsecond=0)
        warm = s.current_c is not None and s.current_c >= s.home_c + COMFORT_C
        ended = s.held != action and s.raise_ended is not None and s.raise_ended >= hour
        if warm or ended:
            return ZoneDecision(ACTION_NORMAL, None, REASON_ZONE_WARM)
        setpoint = aim(action, s.home_c, s.limits, night_delta_c=s.night_delta_c, away_delta_c=s.away_delta_c)
        return ZoneDecision(action, setpoint, REASON_RETURN if action == ACTION_RETURN else REASON_PREHEAT, s.action_until)
    # A lowering, by the relay's run rules; the zone's own cold reading is a safety reason like a cold room.
    safety = s.safety
    if s.current_c is not None and s.current_c < s.home_c - COMFORT_C and safety in (None, SAFETY_SETTLING):
        safety = REASON_ZONE_COLD
    held = s.held == ACTION_LOWER
    decision = relay_decide(
        Situation(
            now=s.now,
            plan_until=s.plan_until if action == ACTION_LOWER else None,
            held_since=s.held_since if held else None,
            last_end=s.last_end,
            lowered_24h=s.lowered_24h,
            writes_today=s.writes_today,
            safety=safety,
            max_writes=MAX_WRITES_DAY,
        )
    )
    if decision.closed:
        return ZoneDecision(ACTION_LOWER, fit(s.home_c - LOWER_C, s.limits), decision.reason, decision.until)
    return ZoneDecision(ACTION_NORMAL, None, decision.reason)


def hour_of(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def forecast_delta(lowerings: Sequence[tuple[datetime, datetime]], kw: float, now: datetime) -> dict[datetime, float]:
    """A metered zone's change to the house's expected load (kW per UTC hour start, from the running hour on): its
    mean `kw` less over each lowering, and the same energy back evenly over `REBOUND` after it (the floor warms up
    again). Lowerings whose rebound is over are left out."""
    out: dict[datetime, float] = {}
    if kw <= 0 or not math.isfinite(kw):
        return out
    current = hour_of(now)

    def add(start: datetime, end: datetime, rate: float) -> None:
        moment = hour_of(start)
        while moment < end:
            overlap = (min(end, moment + timedelta(hours=1)) - max(start, moment)).total_seconds() / 3600
            if overlap > 0 and moment >= current:
                out[moment] = out.get(moment, 0.0) + rate * overlap
            moment += timedelta(hours=1)

    for start, end in lowerings:
        if end <= start or end + REBOUND <= now:
            continue
        hours = (end - start).total_seconds() / 3600
        add(start, end, -kw)
        add(end, end + REBOUND, kw * hours / (REBOUND.total_seconds() / 3600))
    return {hour: round(value, 4) for hour, value in out.items() if abs(value) > 1e-9}
