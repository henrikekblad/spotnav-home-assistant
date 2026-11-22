"""Every load of a site the plan can move, in the common shape (`planning/flexible_load.py`, plan
`plans/v3_flexible_loads_design.md` step 1): each member charger's car, the home battery, the heat pump's control
path, each floor-heating zone and the plain water heater, in that order. Each is described by the module that owns
it (`execution/flexible_car.py`, `battery/flexible.py`, `heat/flexible.py`) from what is already held: no new reads,
no timers, nothing written. Shown in the site's diagnostics (`flexible_loads`) only; no plan reads it yet.

Each load's `state` (`with_states`): `running` where SpotNav steers it, `watched` where it only watches; `paused` with
the reason `ceiling` while the shed order holds it (`site/shed_order.py`: the water heater paused, the heat lowered
one step, a car the import target's hold pauses), `blocked` with the hard rule's code where a step was due and the
load's own rule kept it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ..planning.flexible_load import (
    FlexibleLoad,
    KIND_CAR,
    KIND_HEAT,
    KIND_HEAT_ZONE,
    KIND_WATER_HEATER,
    STATE_BLOCKED,
    STATE_PAUSED,
    STATE_RUNNING,
    STATE_WATCHED,
)
from .import_target import stored_import_target_kw

_LOGGER = logging.getLogger(__name__)


def has_power_tariff(site: Any) -> bool:
    """Whether the site's electricity price has a demand charge or a power level: a peak then costs."""
    from ..pricing.price_source import site_demand_components, site_power_level  # noqa: PLC0415 - it imports the planner

    return bool(site_demand_components(site.hass, site.entry_id)) or site_power_level(site.hass, site.entry_id) is not None


def flexible_loads(site: Any) -> list[FlexibleLoad]:
    """The site's loads (see the module). A part that cannot be described is left out and logged; never raises."""
    from ..battery.flexible import site_battery_load  # noqa: PLC0415 - the parts import the site
    from ..execution.flexible_car import site_car_loads  # noqa: PLC0415
    from ..heat.flexible import heat_loads  # noqa: PLC0415

    try:
        tariff = has_power_tariff(site)
    except Exception:  # noqa: BLE001 - a description never disturbs the site
        _LOGGER.debug("flexible_loads: the power tariff could not be read", exc_info=True)
        tariff = False
    target = stored_import_target_kw(site.config) is not None
    parts: tuple[tuple[str, Callable[[], list[FlexibleLoad]]], ...] = (
        ("car", lambda: site_car_loads(site, power_tariff=tariff, import_target=target)),
        ("home_battery", lambda: [load] if (load := site_battery_load(site)) is not None else []),
        ("heat", lambda: heat_loads(site, power_tariff=tariff)),
    )
    found: list[FlexibleLoad] = []
    for name, part in parts:
        try:
            found.extend(part())
        except Exception:  # noqa: BLE001 - one part's failure leaves the others
            _LOGGER.debug("flexible_loads: the %s part could not be described", name, exc_info=True)
    try:
        return with_states(site, found)
    except Exception:  # noqa: BLE001 - the descriptions stand without their states
        _LOGGER.debug("flexible_loads: the loads' states could not be read", exc_info=True)
        return found


def with_states(site: Any, loads: list[FlexibleLoad]) -> list[FlexibleLoad]:
    """The loads with what each is doing now (see the module)."""
    from ..site.shed_order import LOAD_HEAT, LOAD_WATER_HEATER, REASON_CEILING  # noqa: PLC0415

    order = getattr(site, "shed_order", None)
    steps = {} if order is None else dict(order.steps)
    blocked = {} if order is None else dict(order.blocked)
    paused_hour = getattr(site, "_target_paused_hour", {}) or {}
    budget = getattr(site, "_target_budget", None)
    out: list[FlexibleLoad] = []
    for load in loads:
        shed = {KIND_WATER_HEATER: LOAD_WATER_HEATER, KIND_HEAT: LOAD_HEAT, KIND_HEAT_ZONE: LOAD_HEAT}.get(load.kind)
        state, reason = (STATE_RUNNING if load.controlled else STATE_WATCHED), None
        step = None if shed is None else steps.get(shed)
        if step is not None and (not step.live or not load.controlled):
            # Watched: what it would do.
            reason = REASON_CEILING
        elif step is not None and step.taken and _held(site, load):
            state, reason = STATE_PAUSED, REASON_CEILING
        elif shed is not None and shed in blocked and load.controlled:
            state, reason = STATE_BLOCKED, blocked[shed]
        elif load.kind == KIND_CAR and budget is not None and paused_hour.get(load.id) == budget.hour_start:
            state, reason = STATE_PAUSED, REASON_CEILING
        out.append(replace(load, state=state, state_reason=reason))
    return out


def _held(site: Any, load: FlexibleLoad) -> bool:
    """Whether the load's own control holds it now (the water heater off, the pump lowered, the zone lowered): a step
    its own rules did not take leaves it running."""
    if load.kind == KIND_WATER_HEATER:
        heater = getattr(site, "water_heater_control", None)
        return bool(heater is not None and heater.held_off)
    if load.kind == KIND_HEAT:
        control = getattr(site, "heat_control", None)
        return bool(control is not None and control.held)
    zones = getattr(site, "zones_control", None)
    zone = None if zones is None else zones.zones.get(load.id)
    return bool(zone is not None and zone.held_lower())
