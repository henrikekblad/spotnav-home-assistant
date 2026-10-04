"""A site's last hour, one sample a minute, for the debug bundle (`history_60min`).

In memory only and cheap: each sample reads what the site controller and its member chargers already
hold (no recorder query, no service call). A sample is the grid's total power, the site's current per
phase, the battery's power and, per member charger, its measured current, charge control, connection
and connector status and the car's state of charge. No PV power: a site knows no PV sensor, so there is
none to state. Never raises; a charger that cannot be read is a sample with `error`.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING, Any, Final

from homeassistant.util import dt as dt_util

from ..const import CONF_CHARGER_ENTRY_IDS
from .site_capacity import PHASES

if TYPE_CHECKING:
    from .site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

#: Seconds between two samples.
SAMPLE_INTERVAL_S: Final = 60
#: Samples kept: the last 60 minutes.
SAMPLE_COUNT: Final = 60


def _rounded(value: Any, digits: int = 2) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), digits)


class SiteHistory:
    """The bounded ring of minute samples of one site, oldest first."""

    def __init__(self, length: int = SAMPLE_COUNT) -> None:
        self._samples: deque[dict[str, Any]] = deque(maxlen=length)

    @property
    def samples(self) -> list[dict[str, Any]]:
        """A copy of the samples, oldest first."""
        return [dict(sample) for sample in self._samples]

    def sample(self, site: SiteCapacityController) -> None:
        """Take one sample of `site` now. A failure is logged and skipped, never raised."""
        try:
            self._samples.append(site_sample(site))
        except Exception:  # noqa: BLE001 - a diagnostic sample must never disturb the site
            _LOGGER.debug("Could not sample the site's history", exc_info=True)


def _grid(site: SiteCapacityController) -> tuple[float | None, str | None]:
    """The grid's total power (W, positive = import) and where it came from: the meter's total (`total`),
    else the sum of the three phases' signed active power (`phases`), else unknown."""
    total = site.grid_total_power()
    if total is not None:
        return _rounded(total.value, 1), "total"
    phases = site.result.phase_signed_active_power_w
    values = [phases.get(phase) for phase in PHASES]
    if values and all(isinstance(value, (int, float)) for value in values):
        return _rounded(sum(values), 1), "phases"  # type: ignore[arg-type]
    return None, None


def site_sample(site: SiteCapacityController) -> dict[str, Any]:
    """One sample of `site` (module docstring)."""
    grid_w, grid_source = _grid(site)
    battery = site.battery_aggregate_power()
    sample: dict[str, Any] = {
        "time": dt_util.utcnow().isoformat(),
        "grid_w": grid_w,
        "grid_source": grid_source,
        "site_current_a": {
            phase: _rounded(value) for phase, value in site.result.measured_phase_current_a.items()
        },
        "battery_w": None if battery is None else _rounded(battery.value, 1),
        "chargers": {},
    }
    for charger_entry_id in site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
        try:
            sample["chargers"][charger_entry_id] = _charger_sample(site, charger_entry_id)
        except Exception as err:  # noqa: BLE001 - one charger must not cost the sample
            sample["chargers"][charger_entry_id] = {"error": type(err).__name__}
    return sample


def _charger_sample(site: SiteCapacityController, charger_entry_id: str) -> dict[str, Any]:
    from ..runtime import charger_data, domain_data

    measured = site.charger_measured_current(charger_entry_id)
    item: dict[str, Any] = {
        "current_a": None
        if measured is None
        else {phase: _rounded(measured.get(phase).value) for phase in PHASES},
    }
    data = charger_data(site.hass, charger_entry_id)
    if data is None:
        item["loaded"] = False
        return item
    controller = data.controller
    control = site.hass.states.get(controller.charge_control)
    item.update(
        charge_control=None if control is None else control.state,
        charging=controller.charging,
        connection=controller.connection()[0],
        connector_status=controller.charge_progress_facts().connector_status,
    )
    store = domain_data(site.hass).auto_store
    vehicle_id = None if store is None else store.settings(charger_entry_id).target.vehicle_id
    reading = data.soc_reader.read(vehicle_id)
    item["soc_percent"] = None if reading is None else _rounded(reading.soc_percent, 1)
    item["soc_source"] = None if reading is None else reading.source
    return item
