"""The battery's own heater: tell the person when a cold battery's preheating is off, and work out, in shadow, the
preheating SpotNav would ask for ahead of a planned charge (`plans/v3_sigen_preheat.md`).

Pure: no `hass`, no clock. Nothing here writes, and there is no write path for preheating anywhere: the recipe is not
field tested (`RECIPE_FIELD_TESTED`), and writing waits for a field test on a cold plant and a separate safety review.

* **Cold** (`COLD_C`, `WARM_C`): an LFP cell below 5 °C takes about a tenth of its capacity per hour at most, so a
  battery that cold loses the cheap hours; it counts as warm again from 7 °C (the gap keeps cells warming as they
  charge from flickering the line).
* **The line** (`PreheatWatch`): while SpotNav runs the battery, its cells are cold and its preheating switch is off
  (or disabled in Home Assistant, so it cannot be read), for `SHOW_AFTER` in a row: one line in the battery section
  saying so and where to turn it on. It clears itself as soon as any of that stops. A switch that reads unavailable
  (the plant has no such register: the protocol names only Sigen PV M1-HYA/HYB) shows nothing; it is counted.
* **The charge derate** (`charge_derate`, a guess): the share of the battery's charge power the plan counts on at a
  cell temperature: 1 from `DERATE_FULL_C`, 0.5 from 5 °C, 0.2 from 0 °C, nothing below. From LFP cell sheets (0-5 °C
  about 0.1 C, 5-15 °C 0.3-0.5 C, warmer up to 1 C) against a home module rated about 0.5 C. The plant's BMS decides
  what it really takes; the plan only stops counting on power it will not get.
* **The preheating SpotNav would ask for** (`shadow_request`): while cells are below `DERATE_FULL_C` and the plan
  charges the battery (from the grid or the sun), one time slot per coming run of charging slots (at most the
  recipe's slots), each its start, end and highest planned kW, with the heater on, its mode manual and "advance" on,
  so the plant warms the cells before the run. Sigenergy's registers say "epoch seconds with time zone to local
  time", which is either the plain epoch or the local wall clock written as if it were UTC: both are worked out
  (`epoch_utc`, `epoch_local`), and `epoch_local` is what would be written until a tester shows otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Protocol

from .facts import BatteryFacts

#: Below this (°C) a battery's cells are cold (see the module).
COLD_C: Final = 5.0
#: From this (°C) they are warm again.
WARM_C: Final = 7.0
#: How long the condition must hold before the line shows.
SHOW_AFTER: Final = timedelta(minutes=30)
#: From this (°C) the plan counts on the battery's whole charge power.
DERATE_FULL_C: Final = 10.0
#: The charge derate's steps: (from °C, share of the charge power), warmest first; below the last, nothing.
DERATE_STEPS: Final[tuple[tuple[float, float], ...]] = ((DERATE_FULL_C, 1.0), (5.0, 0.5), (0.0, 0.2))
#: A planned battery power at or above this (kW) is a charge.
CHARGE_MIN_KW: Final = 0.1
#: Whether the preheating recipe was tested on a real plant: until it is, nothing is written (and nothing can be).
RECIPE_FIELD_TESTED: Final = False

#: The line's codes: the switch is off, or it is disabled in Home Assistant.
LINE_OFF: Final = "off"
LINE_DISABLED: Final = "disabled"


def charge_derate(cell_c: float | None) -> float:
    """The share of the charge power the plan counts on at `cell_c`; 1 when the temperature is not known."""
    if cell_c is None:
        return 1.0
    for from_c, share in DERATE_STEPS:
        if cell_c >= from_c:
            return share
    return 0.0


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


class _Planned(Protocol):
    start: datetime
    end: datetime
    action: str
    battery_kw: float


@dataclass(frozen=True, slots=True)
class PreheatSlot:
    """One time slot SpotNav would ask the heater for: a coming run of charging."""

    start: datetime
    end: datetime
    power_kw: float

    @staticmethod
    def _local_epoch(moment: datetime, tz: Any) -> int:
        local = moment.astimezone(tz)
        offset = local.utcoffset() or timedelta(0)
        return int(moment.timestamp() + offset.total_seconds())

    def as_dict(self, tz: Any) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "power_kw": round(self.power_kw, 3),
            "epoch_utc": [int(self.start.timestamp()), int(self.end.timestamp())],
            "epoch_local": [self._local_epoch(self.start, tz), self._local_epoch(self.end, tz)],
        }


def charge_runs(plan: Sequence[_Planned], now: datetime, limit: int) -> list[PreheatSlot]:
    """The plan's runs of charging slots (battery power `CHARGE_MIN_KW` or more, from the grid or the sun) that end
    after `now`, at most `limit`, each with its highest planned kW."""
    runs: list[PreheatSlot] = []
    for slot in sorted(plan, key=lambda item: item.start):
        if slot.end <= now or slot.battery_kw < CHARGE_MIN_KW:
            continue
        last = runs[-1] if runs else None
        if last is not None and last.end == slot.start:
            runs[-1] = PreheatSlot(last.start, slot.end, max(last.power_kw, slot.battery_kw))
        elif len(runs) < limit:
            runs.append(PreheatSlot(slot.start, slot.end, slot.battery_kw))
        else:
            break
    return runs


def shadow_request(
    plan: Sequence[_Planned], facts: BatteryFacts, now: datetime, tz: Any = timezone.utc, slots: int = 30,
    manual_option: str | None = "Manual",
) -> dict[str, Any] | None:
    """What SpotNav would write to the battery's heater now (see the module), never written; `None` when the cells
    are warm or not read, nothing is planned to charge, or the battery has no heater SpotNav knows."""
    heater = facts.preheat
    cell = facts.cell_temperature_c
    if heater is None or cell is None or cell >= DERATE_FULL_C:
        return None
    runs = charge_runs(plan, now, slots)
    if not runs:
        return None
    entities = heater.entities
    writes: list[dict[str, Any]] = [
        {"register": 50000, "role": "enable", "entity_id": heater.enable_entity, "value": "on"},
        {"register": 50001, "role": "mode", "entity_id": entities.get("mode"), "value": manual_option},
        {"register": 50002, "role": "advance", "entity_id": entities.get("advance"), "value": "on"},
    ]
    for number, run in enumerate(runs, start=1):
        times = run.as_dict(tz)
        base = 50003 + 6 * (number - 1)
        writes += [
            {"register": base, "role": f"slot_{number}_start", "entity_id": entities.get(f"slot_{number}_start"),
             "value": times["epoch_local"][0]},
            {"register": base + 2, "role": f"slot_{number}_end", "entity_id": entities.get(f"slot_{number}_end"),
             "value": times["epoch_local"][1]},
            {"register": base + 4, "role": f"slot_{number}_power", "entity_id": entities.get(f"slot_{number}_power"),
             "value": round(run.power_kw, 3)},
        ]
    return {
        "cell_c": round(cell, 1),
        "derate": charge_derate(cell),
        "switch": heater.state,
        "slots": [run.as_dict(tz) for run in runs],
        "writes": writes,
        "missing": sorted(
            {write["role"] for write in writes if write["entity_id"] is None}
            | {role for role in heater.disabled if any(write["role"] == role for write in writes)}
        ),
        "field_tested": RECIPE_FIELD_TESTED,
        "written": False,
    }


def request_key(request: dict[str, Any] | None) -> tuple[Any, ...] | None:
    """What makes a request another one for the change log: its slots' times and kW (the temperature leaves it)."""
    if request is None:
        return None
    return tuple((slot["start"], slot["end"], slot["power_kw"]) for slot in request["slots"])
