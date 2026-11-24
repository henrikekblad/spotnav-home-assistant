"""Starting the charge in the car: the car's own charge switch, turned on once when the car refuses the current
the charger offers (`execution/car_start.py` decides when).

A Tesla can be in a stopped state of its own (stopped from the Tesla app, or by a service linked to the car): the
charger offers current, the connector says the car asks for none (`SuspendedEV`), and only a start from the car's
side clears it. Its integration offers that start as a switch on the car's device (`CHARGE_SWITCH_SUFFIXES`);
SpotNav turns it on, and nothing else: never off, and nothing is restored afterwards. The switch's state is not the
car's enable flag (it reads on only while the car charges), so it is never read as a condition; only whether the
switch is there and available.

Which integrations: only those whose switch was read in their own source (`CHARGE_SWITCH_SUFFIXES`). A car of another
integration has no switch here, and the person is told to start the charge in the car's app instead.

Only for the car at this charger (`car_here`): its identification settled for this plug-in, and, where the car has
them, its own plug sensor saying plugged in and its tracker saying home. Anything less (a question open, a guess, an
unknown reading) is no start, and nothing new is told either.

How often: at most once per plug-in (`plug_in`, the controller's plug-in instant; with none known, never), never
within `CHARGE_START_MIN_INTERVAL_S` of the last start of the same car, and at most `CHARGE_START_DAILY_CAP` in a day,
whatever the results. The record is stored
(`DECISION_DOMAIN_VEHICLE_CHARGE_START`) before the call is made, so a restart, or a restart loop, cannot repeat it.
The same record keeps the plug-in whose notice was told (`notice_plug_in`), so a restart does not tell it again. It is
also what diagnostics show: when, which switch, the result.

Never an error: a call that fails, times out or cannot be made is logged and noted; the charge's own end (the car
ended it) goes on as without it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ..runtime import domain_data
from .discovery_decisions import DECISION_DOMAIN_VEHICLE_CHARGE_START


_LOGGER = logging.getLogger(__name__)

#: The integrations whose charge switch was read in Home Assistant core's source (`switch.py`, 2026-11-24), by the
#: suffix of its unique id (`<vin>-<key>`), and nothing else. Each one's `is_on` reads the car's `charging_state`
#: (Starting or Charging), not whether the car's own charge is enabled; turning it on starts the charge in the car:
#: * `tessie`: `charge_state_charge_enable_request`, `start_charging`;
#: * `teslemetry`, `tesla_fleet`: `charge_state_user_charge_enable_request`, `charge_start()`.
CHARGE_SWITCH_SUFFIXES: Final[dict[str, str]] = {
    "tessie": "-charge_state_charge_enable_request",
    "teslemetry": "-charge_state_user_charge_enable_request",
    "tesla_fleet": "-charge_state_user_charge_enable_request",
}

#: Minimum time between two starts in the same car, whatever started them and whatever they answered: a replug
#: shortly after, or a restart, does not start it again. Teslemetry bills commands and Tesla Fleet limits them.
CHARGE_START_MIN_INTERVAL_S: Final = 30 * 60.0

#: At most this many starts in one car in `CHARGE_START_DAY_S`, whatever started them: a plug-in that flaps (a
#: connection that drops to "no vehicle" now and then) is no reason for a command each time.
CHARGE_START_DAILY_CAP: Final = 3
CHARGE_START_DAY_S: Final = 24 * 3600.0

#: How long the call is waited for before it counts as timed out (the integration may wake the car first).
CHARGE_START_TIMEOUT_S: Final = 2 * 60.0

#: Results, as stored and shown in diagnostics.
RESULT_ASKED: Final = "asked"  # the call was made and has not answered (or Home Assistant stopped meanwhile)
RESULT_OK: Final = "ok"
RESULT_FAILED: Final = "failed"
RESULT_TIMEOUT: Final = "timeout"
#: Results of a start not made (not stored).
SKIP_NO_SWITCH: Final = "no_switch"
SKIP_SAME_PLUG_IN: Final = "same_plug_in"
SKIP_TOO_SOON: Final = "too_soon"
SKIP_BUSY: Final = "busy"
SKIP_NO_PLUG_IN: Final = "no_plug_in"
SKIP_DAILY_CAP: Final = "daily_cap"

_IN_FLIGHT: Final = "spotnav_vehicle_charge_start_in_flight"


@dataclass(frozen=True, slots=True)
class ChargeSwitch:
    """A car's charge switch: its integration and entity."""

    integration: str
    entity_id: str


def _switch_entries(hass: HomeAssistant, vehicle_id: object) -> list[er.RegistryEntry]:
    if not isinstance(vehicle_id, str) or not vehicle_id or dr.async_get(hass).async_get(vehicle_id) is None:
        return []
    return [
        entry
        for entry in er.async_entries_for_device(er.async_get(hass), vehicle_id)
        if entry.domain == "switch"
        and entry.platform in CHARGE_SWITCH_SUFFIXES
        and entry.disabled_by is None
        and entry.unique_id.endswith(CHARGE_SWITCH_SUFFIXES[entry.platform])
    ]


def has_charge_switch(hass: HomeAssistant, vehicle_id: object) -> bool:
    """Whether the car has a charge switch SpotNav knows, whatever it reads now (the car's start and its notice are
    only for such a car; any other keeps the plain "the car takes no current" and the car's end)."""
    return bool(_switch_entries(hass, vehicle_id))


def car_here(hass: HomeAssistant, entry_id: str, vehicle_id: str | None) -> bool:
    """Whether `vehicle_id` is confirmed to be the car at this charger now: its identification is settled for this
    plug-in (`VehicleIdentifier.confirms`), and the car's own plug source says plugged in and its location source
    says home, each where the car has one (a source that reads nothing, or unavailable, is no confirmation)."""
    from ..runtime import charger_data  # noqa: PLC0415 - runtime reaches the controller, which imports this module
    from .identification_sources import identification_sources, location_reading, source_plug_reading  # noqa: PLC0415

    if not vehicle_id:
        return False
    data = charger_data(hass, entry_id)
    identifier = None if data is None else data.identifier
    if identifier is None or not identifier.confirms(vehicle_id):
        return False
    plug, location = identification_sources(hass, vehicle_id)
    if plug.entity_id is not None and source_plug_reading(hass, hass.states.get(plug.entity_id)) is not True:
        return False
    if location.entity_id is not None and location_reading(hass.states.get(location.entity_id)) is not True:
        return False
    return True


def charge_switch(hass: HomeAssistant, vehicle_id: object) -> ChargeSwitch | None:
    """The car's charge switch, when its device has one of `CHARGE_SWITCH_SUFFIXES`, enabled and reading on or off
    (an unavailable switch reaches no car), else `None`."""
    for entry in _switch_entries(hass, vehicle_id):
        state = hass.states.get(entry.entity_id)
        if state is None or state.state not in (STATE_ON, STATE_OFF):
            return None
        return ChargeSwitch(entry.platform, entry.entity_id)
    return None


def last_charge_start(hass: HomeAssistant, vehicle_id: str) -> dict[str, Any] | None:
    """This car's record as stored (`{"at", "integration", "entity_id", "plug_in", "result", "notice_plug_in"}`, each
    key only once it was written), or `None`."""
    store = domain_data(hass).decision_store
    return None if store is None else store.confirmed_payload(DECISION_DOMAIN_VEHICLE_CHARGE_START, vehicle_id)


def _instant(value: Any) -> datetime | None:
    parsed = dt_util.parse_datetime(value) if isinstance(value, str) else None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def _calls_today(record: dict[str, Any] | None, now: datetime) -> list[str]:
    """The starts of the last `CHARGE_START_DAY_S` as stored (`calls`, ISO instants)."""
    calls = [] if record is None else record.get("calls")
    if not isinstance(calls, list):
        return []
    kept = []
    for value in calls:
        at = _instant(value)
        if at is not None and (now - at).total_seconds() < CHARGE_START_DAY_S:
            kept.append(value)
    return kept


def charge_start_skip_reason(hass: HomeAssistant, vehicle_id: str | None, plug_in: str | None) -> str | None:
    """Why the charge is not started in this car now, or `None` when it may be. `plug_in` names the plug-in (the
    controller's plug-in instant, `None` when not known: only the interval holds then)."""
    if not vehicle_id or charge_switch(hass, vehicle_id) is None:
        return SKIP_NO_SWITCH
    if plug_in is None:
        # Nothing names the plug-in, so nothing could keep a restart from starting it again.
        return SKIP_NO_PLUG_IN
    if vehicle_id in hass.data.setdefault(_IN_FLIGHT, set()):
        return SKIP_BUSY
    record = last_charge_start(hass, vehicle_id)
    if record is not None and record.get("plug_in") == plug_in and "at" in record:
        return SKIP_SAME_PLUG_IN
    now = dt_util.utcnow()
    if record is not None and "at" in record:
        last = _instant(record.get("at"))
        # A stored instant that cannot be read is no proof the interval passed.
        if last is None or (now - last).total_seconds() < CHARGE_START_MIN_INTERVAL_S:
            return SKIP_TOO_SOON
    if len(_calls_today(record, now)) >= CHARGE_START_DAILY_CAP:
        return SKIP_DAILY_CAP
    return None


async def _note(hass: HomeAssistant, vehicle_id: str, changes: dict[str, Any]) -> None:
    """Merge `changes` into the car's record (the notice's plug-in and the start's fields are kept side by side).
    Raises when there is no store: nothing is ever sent that is not recorded first."""
    store = domain_data(hass).decision_store
    if store is None:
        raise RuntimeError("no decision store")
    await store.async_confirm(
        DECISION_DOMAIN_VEHICLE_CHARGE_START, vehicle_id, {**(last_charge_start(hass, vehicle_id) or {}), **changes}
    )


def stored_notice(hass: HomeAssistant, vehicle_id: str | None, plug_in: str | None) -> dict[str, Any] | None:
    """The "the car takes no current" notice told for this car's plug-in, kept across a restart: `{"at",
    "soc_percent", "target", "line"}` (`line`: the status line still stands, the car has not drawn since), or `None`
    when none was told for this plug-in."""
    if not vehicle_id or plug_in is None:
        return None
    record = last_charge_start(hass, vehicle_id)
    if record is None or record.get("notice_plug_in") != plug_in:
        return None
    return {
        "at": _instant(record.get("notice_at")),
        "soc_percent": record.get("notice_soc_percent"),
        "target": record.get("notice_target"),
        "line": record.get("notice_line") is not False,
    }


def notice_told(hass: HomeAssistant, vehicle_id: str | None, plug_in: str | None) -> bool:
    """Whether the "the car takes no current" notice was told for this car's plug-in (kept across a restart)."""
    return stored_notice(hass, vehicle_id, plug_in) is not None


async def async_note_notice(
    hass: HomeAssistant,
    vehicle_id: str | None,
    plug_in: str | None,
    *,
    at: datetime | None = None,
    soc_percent: float | None = None,
    target: float | None = None,
    line: bool = True,
) -> None:
    """Remember the notice told for this car's plug-in, with when, its level and target, and whether its status line
    still stands (`line` `False` once the car drew). Never raises."""
    if not vehicle_id or plug_in is None:
        return
    changes: dict[str, Any] = {"notice_plug_in": plug_in, "notice_line": line}
    if at is not None:
        changes.update({"notice_at": at.isoformat(), "notice_soc_percent": soc_percent, "notice_target": target})
    try:
        await _note(hass, vehicle_id, changes)
    except Exception as err:  # noqa: BLE001 - a note is never SpotNav's error
        _LOGGER.debug("Noting the car's notice failed: %s", type(err).__name__)


async def async_start_charge_in_car(hass: HomeAssistant, vehicle_id: str, plug_in: str | None) -> str:
    """Turn the car's charge switch on, when it may be (`charge_start_skip_reason`); return the result or the reason
    it was not. Only `switch.turn_on`, ever.

    Never raises. The record is stored before the call so an interrupted call still counts against the interval.
    """
    try:
        reason = charge_start_skip_reason(hass, vehicle_id, plug_in)
        if reason is not None:
            _LOGGER.debug("Not starting the charge in the car: %s", reason)
            return reason
        switch = charge_switch(hass, vehicle_id)
        assert switch is not None
        in_flight: set[str] = hass.data.setdefault(_IN_FLIGHT, set())
        in_flight.add(vehicle_id)
    except Exception as err:  # noqa: BLE001 - a start in the car is never SpotNav's error
        _LOGGER.debug("Could not decide on starting the charge in the car: %s", type(err).__name__)
        return RESULT_FAILED
    now = dt_util.utcnow()
    record = {
        "at": now.isoformat(),
        "integration": switch.integration,
        "entity_id": switch.entity_id,
        "plug_in": plug_in,
        "result": RESULT_ASKED,
        "calls": [*_calls_today(last_charge_start(hass, vehicle_id), now), now.isoformat()],
    }
    try:
        # Recorded first (no store, no record: no call); a failure here is no start.
        await _note(hass, vehicle_id, record)
        _LOGGER.info(
            "The car takes no current the charger offers: starting the charge in the car through %s (%s)",
            switch.integration,
            switch.entity_id,
        )
        try:
            async with asyncio.timeout(CHARGE_START_TIMEOUT_S):
                await hass.services.async_call(
                    "switch", "turn_on", {"entity_id": switch.entity_id}, blocking=True
                )
            result = RESULT_OK
        except TimeoutError:
            result = RESULT_TIMEOUT
        except Exception as err:  # noqa: BLE001 - another integration's failure is not ours to raise
            _LOGGER.info("Starting the charge in the car through %s failed: %s", switch.integration, type(err).__name__)
            result = RESULT_FAILED
        try:
            await _note(hass, vehicle_id, {"result": result})
        except Exception as err:  # noqa: BLE001 - the call was made; only its result is not noted
            _LOGGER.debug("Noting the start's result failed: %s", type(err).__name__)
    except Exception as err:  # noqa: BLE001 - a start in the car is never SpotNav's error
        _LOGGER.debug("Starting the charge in the car failed: %s", type(err).__name__)
        result = RESULT_FAILED
    finally:
        in_flight.discard(vehicle_id)
    if result == RESULT_TIMEOUT:
        _LOGGER.info(
            "Starting the charge in the car through %s did not answer within %.0f s",
            switch.integration,
            CHARGE_START_TIMEOUT_S,
        )
    return result


def charge_start_diagnostics(hass: HomeAssistant) -> dict[str, Any]:
    """Every car's last start of the charge in the car and last notice, by vehicle id, with its switch now."""
    store = domain_data(hass).decision_store
    if store is None:
        return {}
    out: dict[str, Any] = {}
    for vehicle_id in store.confirmed_ids(DECISION_DOMAIN_VEHICLE_CHARGE_START):
        switch = charge_switch(hass, vehicle_id)
        out[vehicle_id] = {
            "last": last_charge_start(hass, vehicle_id),
            "switch": None if switch is None else switch.entity_id,
        }
    return out
