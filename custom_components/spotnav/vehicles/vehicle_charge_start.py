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

How often: at most once per plug-in (`plug_in`, the controller's plug-in instant) and never within
`CHARGE_START_MIN_INTERVAL_S` of the last start of the same car, whatever the result. The record is stored
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

_IN_FLIGHT: Final = "spotnav_vehicle_charge_start_in_flight"


@dataclass(frozen=True, slots=True)
class ChargeSwitch:
    """A car's charge switch: its integration and entity."""

    integration: str
    entity_id: str


def charge_switch(hass: HomeAssistant, vehicle_id: object) -> ChargeSwitch | None:
    """The car's charge switch, when its device has one of `CHARGE_SWITCH_SUFFIXES`, enabled and reading on or off
    (an unavailable switch reaches no car), else `None`."""
    if not isinstance(vehicle_id, str) or not vehicle_id or dr.async_get(hass).async_get(vehicle_id) is None:
        return None
    for entry in er.async_entries_for_device(er.async_get(hass), vehicle_id):
        suffix = CHARGE_SWITCH_SUFFIXES.get(entry.platform)
        if entry.domain != "switch" or suffix is None or entry.disabled_by is not None:
            continue
        if not entry.unique_id.endswith(suffix):
            continue
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


def _last_at(record: dict[str, Any] | None) -> datetime | None:
    at = None if record is None else record.get("at")
    return dt_util.parse_datetime(at) if isinstance(at, str) else None


def charge_start_skip_reason(hass: HomeAssistant, vehicle_id: str | None, plug_in: str | None) -> str | None:
    """Why the charge is not started in this car now, or `None` when it may be. `plug_in` names the plug-in (the
    controller's plug-in instant, `None` when not known: only the interval holds then)."""
    if not vehicle_id or charge_switch(hass, vehicle_id) is None:
        return SKIP_NO_SWITCH
    if vehicle_id in hass.data.setdefault(_IN_FLIGHT, set()):
        return SKIP_BUSY
    record = last_charge_start(hass, vehicle_id)
    if plug_in is not None and record is not None and record.get("plug_in") == plug_in and "at" in record:
        return SKIP_SAME_PLUG_IN
    last = _last_at(record)
    if last is not None and (dt_util.utcnow() - last).total_seconds() < CHARGE_START_MIN_INTERVAL_S:
        return SKIP_TOO_SOON
    return None


async def _note(hass: HomeAssistant, vehicle_id: str, changes: dict[str, Any]) -> None:
    """Merge `changes` into the car's record (the notice's plug-in and the start's fields are kept side by side)."""
    store = domain_data(hass).decision_store
    if store is not None:
        await store.async_confirm(
            DECISION_DOMAIN_VEHICLE_CHARGE_START, vehicle_id, {**(last_charge_start(hass, vehicle_id) or {}), **changes}
        )


def notice_told(hass: HomeAssistant, vehicle_id: str | None, plug_in: str | None) -> bool:
    """Whether the "the car takes no current" notice was told for this car's plug-in (kept across a restart)."""
    if not vehicle_id or plug_in is None:
        return False
    record = last_charge_start(hass, vehicle_id)
    return record is not None and record.get("notice_plug_in") == plug_in


async def async_note_notice(hass: HomeAssistant, vehicle_id: str | None, plug_in: str | None) -> None:
    """Remember that the notice was told for this car's plug-in. Never raises."""
    if not vehicle_id or plug_in is None:
        return
    try:
        await _note(hass, vehicle_id, {"notice_plug_in": plug_in})
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
    record = {
        "at": dt_util.utcnow().isoformat(),
        "integration": switch.integration,
        "entity_id": switch.entity_id,
        "plug_in": plug_in,
        "result": RESULT_ASKED,
    }
    try:
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
        await _note(hass, vehicle_id, {"result": result})
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
