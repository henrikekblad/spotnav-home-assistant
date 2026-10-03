"""Whether a charger is charging from the grid right now.

Home-battery planners (Predbat, EMHASS) and automations hold the battery while the car charges from the
grid. This is a pure reading of facts the controller already has: it writes nothing and commands nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..planning.auto_settings import STRATEGY_HYBRID
from ..runtime import domain_data
from .controller import ChargingController
from .power_energy import read_power_w
from .solar_execution import site_controller_for_charger

SOURCE_PLAN_WINDOW: Final = "plan_window"
SOURCE_HYBRID_GRID: Final = "hybrid_grid"
SOURCE_MANUAL: Final = "manual"
#: SpotNav cannot tell who started the charge, and the site imports from the grid while it runs.
SOURCE_GRID_IMPORT: Final = "grid_import"


@dataclass(frozen=True, slots=True)
class GridCharge:
    """One charger's reading: `source` is `None` exactly when it is off."""

    on: bool
    source: str | None = None
    window_end: str | None = None
    power_w: float | None = None

    def attributes(self) -> dict[str, Any]:
        """The entity attributes, only the ones known."""
        attributes: dict[str, Any] = {"source": self.source}
        if self.window_end is not None:
            attributes["window_end"] = self.window_end
        if self.power_w is not None:
            attributes["power_w"] = self.power_w
        return attributes


OFF: Final = GridCharge(False)


def _active_window_end(controller: ChargingController) -> str | None:
    plan = controller.plan
    if plan is None:
        return None
    try:
        windows = plan.windows
    except ValueError:
        return None
    now = dt_util.utcnow()
    for start, end in windows:
        if start <= now < end:
            return end.isoformat()
    return None


def _site_imports(hass: HomeAssistant, entry_id: str) -> bool:
    """Whether the site's meter reads a usable import now; unknown counts as not importing."""
    site = site_controller_for_charger(hass, entry_id)
    if site is None:
        return False
    watts, _state = site.grid_total_reading()
    return watts is not None and watts > 0


def grid_charge(hass: HomeAssistant, entry_id: str, controller: ChargingController) -> GridCharge:
    """Whether this charger is charging with energy that is meant to come from the grid.

    On while it charges and the charge is a manual start, a plan window (cheapest, or hybrid's grid
    part) or one SpotNav cannot attribute while the site imports. Off when it is idle or paused, and
    for solar-surplus charging (the solar strategy, or hybrid's solar part).
    """
    if not controller.charging:
        return OFF
    store = domain_data(hass).auto_store
    settings = None if store is None else store.settings(entry_id)
    hybrid = settings is not None and settings.strategy == STRATEGY_HYBRID
    power_w = read_power_w(hass, controller.power_entity_id)
    window_end = _active_window_end(controller)
    origin = controller.charge_origin
    if origin == "manual":
        return GridCharge(True, SOURCE_MANUAL, window_end, power_w)
    if window_end is not None:
        source = SOURCE_HYBRID_GRID if hybrid else SOURCE_PLAN_WINDOW
        return GridCharge(True, source, window_end, power_w)
    if origin == "solar":
        return OFF
    if origin == "plan_window":
        return GridCharge(True, SOURCE_HYBRID_GRID if hybrid else SOURCE_PLAN_WINDOW, None, power_w)
    if _site_imports(hass, entry_id):
        return GridCharge(True, SOURCE_GRID_IMPORT, None, power_w)
    return OFF
