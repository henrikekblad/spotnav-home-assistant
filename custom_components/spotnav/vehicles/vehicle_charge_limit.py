"""Writing a vehicle's charge limit through the entity its own integration owns.

This is a `number.set_value` on the vehicle integration's charge-limit entity. It does not wake the
car; the limit takes effect when that integration next reads it, so the wording is "set the limit",
never "set it now".

* The entity is resolved, never guessed: `vehicle_discovery` names the charge-limit entity, the
  same decision the dashboard builds its ceiling from, so this writes to exactly the entity a planner
  was shown. No brand or entity name is matched.
* Writes are rate limited per vehicle (a slider invites dragging and every write is a real call in
  the vehicle integration's API). The bucket is separate from `VehicleRefreshLimiter`: a refresh
  and a write must not consume each other's budget.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from homeassistant.core import HomeAssistant

from ..runtime import domain_data
from .vehicle_discovery import _as_float, select_option_percent, vehicle_charge_limit_entity_id
from .vehicle_refresh import UNKNOWN_VEHICLE


_LOGGER = logging.getLogger(__name__)

#: The only services called: the `number` platform's own setter (or `select`'s, for a limit that
#: is a percent picker), never a brand's service and never anything that wakes or polls the car.
CHARGE_LIMIT_SERVICE_DOMAIN = "number"
CHARGE_LIMIT_SERVICE = "set_value"
CHARGE_LIMIT_SELECT_SERVICE = "select_option"

#: Minimum wait between successful writes for one vehicle.
CHARGE_LIMIT_MIN_INTERVAL_S = 60.0

#: One message for every unusable percent (missing, non-numeric, boolean,
#: non-finite, out of range); it names no entity and no bound.
INVALID_PERCENT = "Invalid charge limit"



class VehicleChargeLimitLimited(Exception):
    """Raised when this vehicle's limit was written too recently.

    Carries how long to wait so the caller can say when, not just no.
    """

    def __init__(self, retry_after_s: float) -> None:
        super().__init__("Vehicle charge limit was set too recently")
        self.retry_after_s = retry_after_s


@dataclass(slots=True)
class VehicleChargeLimitLimiter:
    """One write interval per vehicle, in memory only.

    In memory because it is a courtesy to another integration: a restart costs at most one extra call.
    Per vehicle, since two cars are two vendor APIs. The clock is injected so tests do not sleep.
    """

    min_interval_s: float = CHARGE_LIMIT_MIN_INTERVAL_S
    clock: Callable[[], float] = time.monotonic
    last_write_s: dict[str, float] = field(default_factory=dict)

    def retry_after_s(self, vehicle_id: str) -> float | None:
        """Seconds until this vehicle may be written to, or `None` for now.

        Counted from the last *successful* write; a refusal does not extend it.
        """
        last = self.last_write_s.get(vehicle_id)
        if last is None:
            return None
        remaining = self.min_interval_s - (self.clock() - last)
        return remaining if remaining > 0 else None

    def note_write(self, vehicle_id: str) -> None:
        """Record that this vehicle's limit has just been written."""
        self.last_write_s[vehicle_id] = self.clock()


def limiter_for(hass: HomeAssistant) -> VehicleChargeLimitLimiter:
    """The integration-wide limiter, created on first use.

    Integration-wide, not per config entry: the same car can be written through any charger's webhook,
    so the interval follows the car.
    """
    data = domain_data(hass)
    if data.charge_limit_limiter is None:
        data.charge_limit_limiter = VehicleChargeLimitLimiter()
    return data.charge_limit_limiter


def _requested_percent(value: Any) -> float:
    """`value` as a percent a charge limit may be set to, or a ValueError.

    Only the request-level rule: `0 < percent <= 100`, the same window applied to a reading. The
    resolved entity's own `min`/`max` are checked by the caller, since those are that integration's
    statement about itself. A numeric string is accepted (JSON bodies and HA states both carry numbers
    that way); booleans are rejected as booleans, not read as 1 %.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(INVALID_PERCENT)
    try:
        percent = float(value)
    except (TypeError, ValueError):
        raise ValueError(INVALID_PERCENT) from None
    if not math.isfinite(percent) or not 0.0 < percent <= 100.0:
        raise ValueError(INVALID_PERCENT)
    return percent


def _select_option_for(state: Any, value: float) -> str:
    """The option of a percent `select` to write for `value`: the highest option not above it.

    A limit is never set higher than asked. Below the lowest option, or without options, the
    request is invalid.
    """
    options = [] if state is None else state.attributes.get("options") or []
    fitting = [
        (percent, option)
        for option in options
        if isinstance(option, str)
        and (percent := select_option_percent(option)) is not None
        and percent <= value
    ]
    if not fitting:
        raise ValueError(INVALID_PERCENT)
    return max(fitting, key=lambda item: item[0])[1]


def charge_limit_range(hass: HomeAssistant, vehicle_id: object) -> dict[str, float] | None:
    """The percents `async_set_charge_limit` can write for `vehicle_id`: `{"min", "max", "step"}`, or `None`.

    For a `number`, its own `min`, `max` and `step` (whole percent when it states no step); the range check
    reads the same `min`/`max`. A limit is never 0 % (`_requested_percent`), so a range from 0 starts at its
    first step above it. For a percent `select`, its lowest and highest option, stepped by the smallest gap
    between neighbours. `None` when there is no limit to write, or its range cannot be read.
    """
    entity_id = vehicle_charge_limit_entity_id(hass, vehicle_id)
    state = None if entity_id is None else hass.states.get(entity_id)
    if entity_id is None or state is None:
        return None
    if entity_id.startswith("select."):
        percents = sorted(
            {
                percent
                for option in state.attributes.get("options") or []
                if (percent := select_option_percent(option)) is not None and math.isfinite(percent)
            }
        )
        if len(percents) < 2:
            return None
        minimum, maximum = percents[0], percents[-1]
        step = min(high - low for low, high in zip(percents, percents[1:]))
    else:
        minimum = _as_float(state.attributes.get("min"))
        maximum = _as_float(state.attributes.get("max"))
        step = _as_float(state.attributes.get("step"))
        if step is None or not math.isfinite(step) or step <= 0.0:
            step = 1.0
        if minimum is None or maximum is None:
            return None
        if minimum <= 0.0:
            minimum += (math.floor(-minimum / step) + 1) * step
    if not (math.isfinite(minimum) and math.isfinite(maximum)) or not 0.0 < minimum < maximum <= 100.0:
        return None
    return {"min": float(minimum), "max": float(maximum), "step": float(step)}


async def async_set_charge_limit(
    hass: HomeAssistant, vehicle_id: object, percent: Any
) -> None:
    """Set one vehicle's charge limit, or raise why it was not set.

    * `ValueError` with `UNKNOWN_VEHICLE` for every id that is not a vehicle reported right now (one
      message for all, so the action cannot be used to ask which devices exist).
    * `ValueError` with `INVALID_PERCENT` for a percent no limit can be set to.
    * `VehicleChargeLimitLimited` when this vehicle was written too recently.

    Nothing is logged for a refusal. The value must also be inside the resolved entity's declared
    `min`/`max`; a bound that cannot be read fails closed on the number, never on the vehicle. It is one
    blocking `number.set_value`, so `ok` means the write has happened and a dashboard read straight
    afterwards sees the new value (a statement about Home Assistant, not the car).
    """
    entity_id = vehicle_charge_limit_entity_id(hass, vehicle_id)
    if entity_id is None or not isinstance(vehicle_id, str):
        raise ValueError(UNKNOWN_VEHICLE)
    value = _requested_percent(percent)
    state = hass.states.get(entity_id)
    if entity_id.startswith("select."):
        option = _select_option_for(state, value)
        domain, service, data = (
            "select",
            CHARGE_LIMIT_SELECT_SERVICE,
            {"entity_id": entity_id, "option": option},
        )
    else:
        minimum = None if state is None else _as_float(state.attributes.get("min"))
        maximum = None if state is None else _as_float(state.attributes.get("max"))
        if minimum is None or maximum is None or not minimum <= value <= maximum:
            raise ValueError(INVALID_PERCENT)
        domain, service, data = (
            CHARGE_LIMIT_SERVICE_DOMAIN,
            CHARGE_LIMIT_SERVICE,
            {"entity_id": entity_id, "value": value},
        )
    retry_after_s = limiter_for(hass).retry_after_s(vehicle_id)
    if retry_after_s is not None:
        raise VehicleChargeLimitLimited(retry_after_s)
    await hass.services.async_call(domain, service, data, blocking=True)
    limiter_for(hass).note_write(vehicle_id)
    # Deliberately no entity id, device id or value in the log.
    _LOGGER.debug("Wrote a vehicle's charge limit")
