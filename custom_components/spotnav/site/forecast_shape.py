"""The common shape of a forecast on a sensor: `forecast: [{time, value}]`.

This is what the Home Assistant energy optimisers read (HAEO's documented custom format: a list of `{time, value}`
with ISO 8601 times, the value in the sensor's own unit; EMHASS and ApexCharts take the same list through a
template). The sensors that carry it say their unit as the sensor's unit, so the list needs none of its own:

* Price now: the area's major unit per kWh (fees and VAT included), one row per published interval from the
  running one on (15 or 60 minutes), up to two days.
* House load forecast: kW (the whole load where the site has the sun's production, else the import less the
  chargers: `basis`), one row per hour.
* Solar forecast: kW, the hour's mean power (kWh in an hour), one row per hour; the correction's figure once it is
  learnt, else the forecast as the source gave it (`basis`).

Pure. The list is a large attribute, so every sensor that carries it keeps `forecast` out of the recorder
(`_unrecorded_attributes`): the history holds the state only.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

#: Rows shown at most (an interval of 15 minutes for two days is 192), so an attribute never grows without bound.
MAX_ROWS = 200


def standard_forecast(rows: Iterable[tuple[datetime, float | None]], *, digits: int = 3) -> list[dict[str, object]]:
    """`[{time, value}]` in time order from `(start, value)` pairs; rows without a value or with a start that has no
    time zone are left out (an optimiser would read a naive time as local, which is a guess), and at most `MAX_ROWS`."""
    ordered = sorted(
        (start, value) for start, value in rows if value is not None and start.tzinfo is not None
    )
    return [{"time": start.isoformat(), "value": round(value, digits)} for start, value in ordered[:MAX_ROWS]]
