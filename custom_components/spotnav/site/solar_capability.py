"""Whether a site can run solar and hybrid, and if not, the one stable reason.

Solar needs the grid's signed power. A derived site has it per phase. A direct site (per-phase current
only, no direction) has it only when the meter's total grid power is configured
(`CONF_GRID_POWER_SOURCE`). Pure Python, shared by the strategy picker, the dashboard, the capability
snapshot and the solar executor so they cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from ..const import MEASUREMENT_MODE_DERIVED, MEASUREMENT_MODE_DIRECT
from .measurement_source import grid_power_source_from_dict

#: A direct site without the meter's total grid power.
REASON_NEEDS_TOTAL_GRID_POWER: Final = "needs_total_grid_power"
#: No usable measurement at all (a site not configured, or a charger with no site).
REASON_NEEDS_SURPLUS_MEASUREMENT: Final = "needs_solar_surplus_measurement"


@dataclass(frozen=True, slots=True)
class SolarCapability:
    """`capable`, and the stable `reason` it is not (`None` when it is)."""

    capable: bool
    reason: str | None = None


def solar_capability(measurement_mode: Any, grid_power_source: Any) -> SolarCapability:
    """The capability of a site in `measurement_mode` whose stored total is `grid_power_source`."""
    if measurement_mode == MEASUREMENT_MODE_DERIVED:
        return SolarCapability(True)
    if measurement_mode == MEASUREMENT_MODE_DIRECT:
        if grid_power_source_from_dict(grid_power_source) is not None:
            return SolarCapability(True)
        return SolarCapability(False, REASON_NEEDS_TOTAL_GRID_POWER)
    return SolarCapability(False, REASON_NEEDS_SURPLUS_MEASUREMENT)
