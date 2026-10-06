"""Enforcing a target state of charge for one scheduled charge.

Only Home Assistant can see the car's state of charge while a charge runs, so only it can stop
early when the target is reached.

* `decide_target_stop` is pure: a target and a reading in, a decision out.
* `resolve_soc_reading` reads one state and reports what it found, including "the entity exists but
  its value is unusable" as distinct from "no entity".

`SocReading.source` records where a reading came from; a charger-side reading is preferred over the
vehicle's while it is live. Nothing here writes anything; the controller acts on the decision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ..runtime import domain_data
from ..vehicles.vehicle_discovery import (
    _device_entities,
    _percent_battery_sensor_entity_ids,
    valid_soc_percent,
)


# Where a state-of-charge reading came from.
SocSource = Literal["vehicle", "charger"]

#: How old a reading may be and still count as the state of charge rather than something to
#: estimate forward from.
SOC_FRESH_MAX_AGE_S: Final = 180.0

#: A state written no later than this after Home Assistant started was set again by the start (its
#: integration's first answer, or the state restored), not read anew (`SocReading.restored`).
RESTORE_WINDOW_S: Final = 600.0

#: How far above the target an *estimated* state of charge must be before a charge is stopped on
#: the estimate alone.
ESTIMATE_STOP_MARGIN_PERCENT: Final = 1.0


@dataclass(frozen=True, slots=True)
class SocReading:
    """One state-of-charge reading, with where it came from.

    `soc_percent` is `None` when the entity exists but its value is unusable (`unavailable`, `unknown`,
    non-numeric, outside 0..100); that differs from no reading because the entity is still worth
    watching. `entity_id` is internal and never part of a payload. `age_s` comes from the live
    `State.last_updated`.
    """

    soc_percent: float | None
    source: SocSource
    entity_id: str | None
    vehicle_id: str | None
    age_s: float | None
    #: `True` when `soc_percent` is an estimate carried forward from the last fresh reading by
    #: delivered energy (`soc_estimate.resolve_soc`); `age_s` is then the age of that reading.
    estimated: bool = False
    #: `True` when the state was last written as Home Assistant started (`RESTORE_WINDOW_S`): equal to the
    #: kept anchor's value, it is that reading set again, not a new one (`soc_estimate.resolve_soc`).
    restored: bool = False


@dataclass(frozen=True, slots=True)
class TargetDecision:
    """What one reading means for one target: stop, or not, and why."""

    stop: bool
    reason: Literal[
        "reached",
        #: The stop was taken on an estimate (target plus `ESTIMATE_STOP_MARGIN_PERCENT`).
        "reached_estimate",
        "below_target",
        "no_target",
        "no_source",
        "reading_unusable",
        #: The target is at or above the car's own charge limit: the car ends the charge, not SpotNav.
        "vehicle_limit",
    ]


def charge_ceiling_percent(vehicle_max_percent: float | None) -> float:
    """The level a charge can reach: the car's own charge limit (whole percent, as the planner caps a
    target) when it is known, else 100."""
    if vehicle_max_percent is None or not math.isfinite(vehicle_max_percent):
        return 100.0
    return float(min(100, max(0, math.floor(vehicle_max_percent))))


def charges_to_vehicle_limit(target_soc_percent: float | None, vehicle_max_percent: float | None) -> bool:
    """Whether a target asks for everything the car will take: at or above its own charge limit, or 100
    when the limit is unknown. The car then ends the charge itself."""
    if target_soc_percent is None:
        return False
    return target_soc_percent >= charge_ceiling_percent(vehicle_max_percent)


def resolve_soc_reading(
    hass: HomeAssistant,
    *,
    vehicle_id: str | None,
    entity_id: str | None = None,
) -> SocReading | None:
    """The vehicle's state of charge, the fallback source."""
    return _reading_for_entity(hass, entity_id, source="vehicle", vehicle_id=vehicle_id)


def resolve_charger_soc_reading(
    hass: HomeAssistant, charge_control_entity_id: str | None
) -> SocReading | None:
    """The charger's own state-of-charge reading, or `None`.

    Resolved through the charge-control entity's device: its single percent `device_class: battery`
    sensor. Zero or several such sensors are never guessed between. An unusable value is returned with
    `soc_percent` `None`; the caller decides whether to fall back to the vehicle.
    """
    return _reading_for_entity(
        hass,
        charger_soc_entity_id(hass, charge_control_entity_id),
        source="charger",
        vehicle_id=None,
    )


def charger_soc_entity_id(hass: HomeAssistant, charge_control_entity_id: object) -> str | None:
    """The single percent battery sensor on the charger's device, or `None`."""
    if not isinstance(charge_control_entity_id, str) or not charge_control_entity_id:
        return None
    entity_registry = er.async_get(hass)
    control = entity_registry.async_get(charge_control_entity_id)
    if control is None or control.device_id is None:
        return None
    entities = _device_entities(hass, entity_registry, control.device_id)
    candidates = _percent_battery_sensor_entity_ids(entities)
    return candidates[0] if len(candidates) == 1 else None


def _reading_for_entity(
    hass: HomeAssistant,
    entity_id: str | None,
    *,
    source: SocSource,
    vehicle_id: str | None,
) -> SocReading | None:
    """One entity read as a state of charge, with its source made explicit.

    `None` means nothing to read. An unusable value is returned rather than dropped, since
    `decide_target_stop` reports it separately. The age comes from the live state, not the recorder.
    """
    if not entity_id:
        return None
    state: State | None = hass.states.get(entity_id)
    if state is None:
        return None
    return SocReading(
        soc_percent=valid_soc_percent(state),
        source=source,
        entity_id=entity_id,
        vehicle_id=vehicle_id,
        age_s=(dt_util.utcnow() - state.last_updated).total_seconds(),
        restored=_written_at_start(hass, state),
    )


def _written_at_start(hass: HomeAssistant, state: State) -> bool:
    """Whether `state` was last written while Home Assistant was starting (`RESTORE_WINDOW_S`)."""
    started = domain_data(hass).ha_started_at
    return started is not None and state.last_updated <= started + timedelta(seconds=RESTORE_WINDOW_S)


def decide_target_stop(
    *, target_soc_percent: float | None, reading: SocReading | None, to_vehicle_limit: bool = False
) -> TargetDecision:
    """Whether this reading means the charge is over.

    Pure and side-effect free:

    * no target: `no_target`, answered before looking at any reading;
    * a target at or above the car's own charge limit (`to_vehicle_limit`): `vehicle_limit`, never a
      stop: the car ends the charge when it is full, and neither a reading nor an estimate of SpotNav's
      may end it first;
    * no reading or an unusable value: `reading_unusable`, never a stop; the window's own end remains
      the guard;
    * reading >= target: `reached`, compared as floats without rounding (89.6 is below 90);
    * an estimate >= target + `ESTIMATE_STOP_MARGIN_PERCENT` (capped at 100): `reached_estimate`;
    * otherwise `below_target`.
    """
    if target_soc_percent is None:
        return TargetDecision(stop=False, reason="no_target")
    if to_vehicle_limit:
        return TargetDecision(stop=False, reason="vehicle_limit")
    if reading is None or reading.soc_percent is None:
        return TargetDecision(stop=False, reason="reading_unusable")
    if reading.estimated:
        threshold = min(target_soc_percent + ESTIMATE_STOP_MARGIN_PERCENT, 100.0)
        if reading.soc_percent >= threshold:
            return TargetDecision(stop=True, reason="reached_estimate")
        return TargetDecision(stop=False, reason="below_target")
    if reading.soc_percent >= target_soc_percent:
        return TargetDecision(stop=True, reason="reached")
    return TargetDecision(stop=False, reason="below_target")
