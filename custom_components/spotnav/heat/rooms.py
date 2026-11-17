"""The house's measured temperature from the chosen indoor sensors (plan `plans/v3_heating_plan.md` section 15): the
room closest to too cold rules, not the mean, and a room warmed by a local heat source is left out while it lasts.

Pure. Each sensor comes with its value now and its readings of the last `WINDOW` (the shell reads them from the
recorder). A sensor is a **local heat** while

* it rose at least `LOCAL_RISE_C` over the last `WINDOW` while the other readable sensors rose at most
  `QUIET_RISE_C` (their median): a wood stove, the sun through a window; it needs two readable sensors; or
* it is in the same area as an air-to-air heat pump zone that heats now (the pump is a local heat source).

Left-out sensors are listed with why; if every readable sensor is left out, all of them count (a whole house that
warms is no local source). A value outside `PLAUSIBLE` is no reading.

Each sensor may have an offset against the house target (its room's own aim, `heat/config.py`): the coldest room is
the one whose reading less its offset is lowest, and that figure (`relative_c`) is what the house's comfort band is
compared with. `value_c` stays the room's own reading.

Whether the house is **too warm** (`warm_c`) is judged on the same figure, but never on a room that is rising fast
itself: a sensor warmed by sun or a stove reads warm before the house is, and with one sensor (or all of them rising)
the rule above leaves nothing out. The coldest counted room rising `LOCAL_RISE_C` over the window is no warm reading.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

WINDOW: Final = timedelta(minutes=30)
LOCAL_RISE_C: Final = 0.6
QUIET_RISE_C: Final = 0.2
PLAUSIBLE: Final = (5.0, 35.0)

STATUS_OK: Final = "ok"
STATUS_UNAVAILABLE: Final = "unavailable"
STATUS_LOCAL_HEAT: Final = "local_heat"
STATUS_HEAT_PUMP_ROOM: Final = "air_heat_pump"


@dataclass(frozen=True, slots=True)
class SensorReading:
    entity_id: str
    name: str
    value_c: float | None
    #: `(time, °C)` readings of the window, oldest first (the reading in force at its start may come first).
    history: tuple[tuple[datetime, float], ...] = ()
    area_id: str | None = None
    offset_c: float = 0.0


@dataclass(frozen=True, slots=True)
class SensorView:
    entity_id: str
    name: str
    value_c: float | None
    rise_c: float | None
    status: str
    offset_c: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "value_c": None if self.value_c is None else round(self.value_c, 2),
            "rise_c": None if self.rise_c is None else round(self.rise_c, 2),
            "status": self.status,
            "offset_c": self.offset_c,
        }


@dataclass(frozen=True, slots=True)
class Indoor:
    value_c: float | None = None
    coldest: str | None = None
    coldest_name: str | None = None
    sensors: tuple[SensorView, ...] = field(default_factory=tuple)
    #: The coldest room's reading less its offset: what the house's band is compared with.
    relative_c: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "value_c": None if self.value_c is None else round(self.value_c, 2),
            "coldest": self.coldest,
            "coldest_name": self.coldest_name,
            "sensors": [sensor.as_dict() for sensor in self.sensors],
            "relative_c": None if self.relative_c is None else round(self.relative_c, 2),
        }


def plausible(value: float | None) -> bool:
    return value is not None and PLAUSIBLE[0] <= value <= PLAUSIBLE[1]


def rise(reading: SensorReading, now: datetime) -> float | None:
    """How much the sensor rose over the window: now against the reading in force at the window's start (else its
    first in the window); `None` without both."""
    if not plausible(reading.value_c):
        return None
    start = now - WINDOW
    before = [value for moment, value in reading.history if moment <= start and plausible(value)]
    inside = [value for moment, value in reading.history if moment > start and plausible(value)]
    base = before[-1] if before else (inside[0] if inside else None)
    if base is None:
        return None
    return float(reading.value_c) - base  # type: ignore[arg-type]


def indoor(readings: Sequence[SensorReading], now: datetime, heating_areas: frozenset[str] = frozenset()) -> Indoor:
    """The coldest counted room and every sensor's status (see the module)."""
    rises = {reading.entity_id: rise(reading, now) for reading in readings}
    readable = [reading for reading in readings if plausible(reading.value_c)]
    left_out: dict[str, str] = {}
    for reading in readable:
        if reading.area_id is not None and reading.area_id in heating_areas:
            left_out[reading.entity_id] = STATUS_HEAT_PUMP_ROOM
            continue
        own = rises[reading.entity_id]
        others = [rises[other.entity_id] for other in readable if other is not reading and rises[other.entity_id] is not None]
        if own is not None and own >= LOCAL_RISE_C and others and statistics.median(others) <= QUIET_RISE_C:
            left_out[reading.entity_id] = STATUS_LOCAL_HEAT
    counted = [reading for reading in readable if reading.entity_id not in left_out] or readable
    if len(counted) == len(readable):
        left_out = {}
    views = tuple(
        SensorView(
            reading.entity_id,
            reading.name,
            reading.value_c if plausible(reading.value_c) else None,
            rises[reading.entity_id],
            left_out.get(reading.entity_id, STATUS_OK if plausible(reading.value_c) else STATUS_UNAVAILABLE),
            reading.offset_c,
        )
        for reading in readings
    )
    if not counted:
        return Indoor(sensors=views)
    coldest = min(counted, key=lambda reading: (reading.value_c - reading.offset_c, reading.entity_id))  # type: ignore[operator]
    relative = float(coldest.value_c) - coldest.offset_c  # type: ignore[arg-type]
    return Indoor(coldest.value_c, coldest.entity_id, coldest.name, views, relative)


def warm_c(result: Indoor) -> float | None:
    """The figure the band's high edge is compared with (see the module): the coldest counted room less its offset,
    `None` while that room rises fast or its rise is not known."""
    if result.relative_c is None or result.coldest is None:
        return None
    sensor = next((view for view in result.sensors if view.entity_id == result.coldest), None)
    if sensor is None or sensor.rise_c is None or sensor.rise_c >= LOCAL_RISE_C:
        return None
    return result.relative_c
