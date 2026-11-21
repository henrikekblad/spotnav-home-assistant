"""The battery's own heater: tell the person when a cold battery's preheating is off (`plans/v3_sigen_preheat.md`).

Pure: no `hass`, no clock. Nothing here writes, and there is no write path for preheating anywhere.

* **Cold** (`COLD_C`, `WARM_C`): an LFP cell below 5 °C takes about a tenth of its capacity per hour at most, so a
  battery that cold loses the cheap hours; it counts as warm again from 7 °C (the gap keeps cells warming as they
  charge from flickering the line).
* **The line** (`PreheatWatch`): while SpotNav runs the battery, its cells are cold and its preheating switch is off
  (or disabled in Home Assistant, so it cannot be read), for `SHOW_AFTER` in a row: one line in the battery section
  saying so and where to turn it on. It clears itself as soon as any of that stops. A switch that reads unavailable
  (the plant has no such register: the protocol names only Sigen PV M1-HYA/HYB) shows nothing; it is counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from .facts import BatteryFacts

#: Below this (°C) a battery's cells are cold (see the module).
COLD_C: Final = 5.0
#: From this (°C) they are warm again.
WARM_C: Final = 7.0
#: How long the condition must hold before the line shows.
SHOW_AFTER: Final = timedelta(minutes=30)
#: The line's codes: the switch is off, or it is disabled in Home Assistant.
LINE_OFF: Final = "off"
LINE_DISABLED: Final = "disabled"


@dataclass(slots=True)
class PreheatWatch:
    """The battery section's preheating line (see the module): what it shows, since when the condition held, and how
    often it showed and how often a cold battery's switch could not be read."""

    #: When the condition was first seen in this run of looks, and the code it had.
    since: datetime | None = None
    code: str | None = None
    #: The line shown now (`LINE_OFF`, `LINE_DISABLED`), `None` when none is.
    shown: str | None = None
    #: The cell temperature at the last look (°C), for the line's words.
    cell_c: float | None = None
    cold: bool = False
    counts: dict[str, int] = field(default_factory=dict)

    def look(self, now: datetime, facts: BatteryFacts, controlling: bool) -> tuple[str, str | None] | None:
        """One look. Answers a change of the line as `(event, code)` (`shown` or `cleared`) for the log, else
        `None`."""
        cell = facts.cell_temperature_c
        self.cell_c = cell
        if cell is None:
            self.cold = False
        elif cell < COLD_C:
            self.cold = True
        elif cell >= WARM_C:
            self.cold = False
        heater = facts.preheat
        code: str | None = None
        if controlling and self.cold and heater is not None and heater.state != "on":
            code = heater.state
        if code != self.code:
            if code == "unavailable":
                # Nothing the person can do (the plant has no such register): counted, never shown.
                self._count("unreadable")
            self.since, self.code = (now if code is not None else None), code
        wanted = code if code in (LINE_OFF, LINE_DISABLED) and self.since is not None and now - self.since >= SHOW_AFTER else None
        if wanted == self.shown:
            return None
        before, self.shown = self.shown, wanted
        if wanted is None:
            return ("cleared", before)
        self._count(f"shown_{wanted}")
        return ("shown", wanted)

    def _count(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def status(self, facts: BatteryFacts) -> dict[str, Any] | None:
        """The card's line (`status.preheat`): `{code, cell_c, entity_id, device_id}`, `None` when none shows."""
        heater = facts.preheat
        if self.shown is None or heater is None:
            return None
        return {
            "code": self.shown,
            "cell_c": None if self.cell_c is None else round(self.cell_c, 1),
            "entity_id": heater.enable_entity,
            "device_id": heater.device_id,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "since": None if self.since is None else self.since.isoformat(),
            "code": self.code,
            "shown": self.shown,
            "cell_c": self.cell_c,
            "cold": self.cold,
            "counts": dict(self.counts),
        }
