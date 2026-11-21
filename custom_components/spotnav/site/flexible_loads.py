"""Every load of a site the plan can move, in the common shape (`planning/flexible_load.py`, plan
`plans/v3_flexible_loads_design.md` step 1): each member charger's car, the home battery, the heat pump's control
path, each floor-heating zone and the plain water heater, in that order. Each is described by the module that owns
it (`execution/flexible_car.py`, `battery/flexible.py`, `heat/flexible.py`) from what is already held: no new reads,
no timers, nothing written. Shown in the site's diagnostics (`flexible_loads`) only; no plan reads it yet.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..planning.flexible_load import FlexibleLoad
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
    return found
