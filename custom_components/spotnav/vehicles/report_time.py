"""When the car itself made its state-of-charge report, read from its integration's own timestamp.

A state's `last_updated` is when Home Assistant wrote it, not when the car measured it. A cloud integration
writes the value it last fetched again when Home Assistant starts (the field night of 13-14 October: a Kia
EV6's 43 %, hours old, stamped with the restart's instant and taken as fresh). Many car integrations put the
car's own report time on the same device as a timestamp sensor: Kia/Hyundai `last_updated_at`, Renault
`battery_last_activity`, Polestar `last_updated_battery_data`, VW `last_connected`, Ford `lastRefresh`, and a
generic `last_updated` / `last_update` / `last_communication` elsewhere.

`vehicle_report_time` finds one such sensor, by the words of its key only (`entity_keys`, never a brand), and
reads it. It is optional and conservative: no sensor, several that match equally, a key that names something
else (a trip, a position, a service date, a charge's end), a value that is not a timestamp, one far in the
future or a placeholder before 2010 all give `None`, and the reading keeps its `last_updated`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Final

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .entity_keys import entity_key, EntityKey

#: Keys naming the battery's own report time: preferred over a device-wide one.
BATTERY_REPORT_KEYS: Final = (
    "battery last activity",
    "last battery activity",
    "battery last updated",
    "last updated battery",
    "battery last update",
    "last update battery",
)

#: Keys naming the car's report time as a whole (the status the state of charge came in).
REPORT_KEYS: Final = (
    "last updated at",
    "last updated",
    "last update",
    "last update time",
    "data last update",
    "last communication",
    "last connected",
    "last contact",
    "last refresh",
    "lastrefresh",
    "car captured",
    "captured timestamp",
)

#: Words that make a timestamp something other than the car's report: never read as one.
NOT_A_REPORT_WORDS: Final = (
    "trip",
    "odometer",
    "position",
    "location",
    "gps",
    "parking",
    "service",
    "inspection",
    "next",
    "end",
    "start",
    "departure",
    "finish",
    "finished",
    "complete",
    "completion",
    "full",
    "ready",
    "schedule",
    "scheduled",
    "alarm",
    "door",
    "lock",
    "window",
    "tyre",
    "tire",
    "climate",
    "poll",
    "firmware",
    "software",
)

#: A report this far ahead of Home Assistant's clock is a wrong clock, not a report; less is clock skew.
FUTURE_TOLERANCE_S: Final = 300.0

#: Before this a timestamp is a placeholder (an epoch zero), not a report.
_EARLIEST: Final = datetime(2010, 1, 1, tzinfo=timezone.utc)


def report_time_entity_id(hass: HomeAssistant, soc_entity_id: str | None) -> str | None:
    """The timestamp sensor on the state-of-charge entity's device that tells when the car reported, or `None`.

    A battery report key first; otherwise a whole-car report key. Exactly one candidate in the first group that
    has any is taken; several are never guessed between."""
    if not soc_entity_id:
        return None
    registry = er.async_get(hass)
    soc_entry = registry.async_get(soc_entity_id)
    if soc_entry is None or soc_entry.device_id is None:
        return None
    battery: list[str] = []
    whole: list[str] = []
    for entry in er.async_entries_for_device(registry, soc_entry.device_id):
        if entry.domain != "sensor" or entry.entity_id == soc_entity_id or entry.disabled_by is not None:
            continue
        state = hass.states.get(entry.entity_id)
        if state is not None and state.attributes.get("device_class") not in (None, "timestamp"):
            continue
        key = entity_key(entry)
        if key.has_any(NOT_A_REPORT_WORDS):
            continue
        if _names_any(key, BATTERY_REPORT_KEYS):
            battery.append(entry.entity_id)
        elif _names_any(key, REPORT_KEYS):
            whole.append(entry.entity_id)
    for group in (battery, whole):
        if group:
            return group[0] if len(group) == 1 else None
    return None


def _names_any(key: EntityKey, phrases: tuple[str, ...]) -> bool:
    return any(key.has(phrase) or key.contains(phrase.replace(" ", "")) for phrase in phrases)


def read_report_time(hass: HomeAssistant, entity_id: str | None, now: datetime | None = None) -> datetime | None:
    """The car's report time from `entity_id`, or `None` when it is not a usable timestamp (unknown,
    unavailable, not a time with a zone, before 2010, or more than `FUTURE_TOLERANCE_S` ahead). A little ahead
    is clock skew and reads as now."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    parsed = dt_util.parse_datetime(state.state)
    if parsed is None or parsed.tzinfo is None:
        return None
    current = now if now is not None else dt_util.utcnow()
    if parsed < _EARLIEST or parsed > current + timedelta(seconds=FUTURE_TOLERANCE_S):
        return None
    return min(parsed, current)


def vehicle_report_time(
    hass: HomeAssistant, soc_entity_id: str | None, now: datetime | None = None
) -> tuple[str, datetime] | None:
    """`(timestamp entity, report time)` for the car whose state of charge `soc_entity_id` is, or `None`."""
    entity_id = report_time_entity_id(hass, soc_entity_id)
    reported = read_report_time(hass, entity_id, now)
    return None if entity_id is None or reported is None else (entity_id, reported)
