"""The charger's connection status in one vocabulary, whichever way the charger says it.

`normalise_status(platform, raw)` maps a raw status value to one of `CONNECTION_STATES`. Every raw
value that is mapped is listed in a table here; a value that is not listed is `unknown`, never a
guess. The tables compare lower-case text.

* `disconnected`: no vehicle is connected;
* `connected`: a vehicle is connected and nothing is charging (waiting to start, awaiting
  authorisation, ready);
* `charging`: energy is flowing;
* `paused`: a vehicle is connected and the charge is suspended (by the charger, the vehicle or us);
* `finished`: the vehicle stopped (full or target reached) and is still plugged in;
* `error`: the charger reports a fault;
* `unknown`: the charger cannot say, or says something not listed.
"""

from __future__ import annotations

from typing import Final

DISCONNECTED: Final = "disconnected"
CONNECTED: Final = "connected"
CHARGING: Final = "charging"
PAUSED: Final = "paused"
FINISHED: Final = "finished"
ERROR: Final = "error"
UNKNOWN: Final = "unknown"
CONNECTION_STATES: Final = (DISCONNECTED, CONNECTED, CHARGING, PAUSED, FINISHED, ERROR, UNKNOWN)

#: OCPP's connector status, also what Charge Amps and the OCPP-like DEFA sensor say.
OCPP_STATUS: Final[dict[str, str]] = {
    "available": DISCONNECTED,
    "preparing": CONNECTED,
    "charging": CHARGING,
    "suspendedev": PAUSED,
    "suspendedevse": PAUSED,
    "finishing": FINISHED,
    "faulted": ERROR,
    # Reserved and Unavailable say nothing about a vehicle or a charge.
}

_GOE: Final[dict[str, str]] = {
    "1": DISCONNECTED,
    "idle": DISCONNECTED,
    "ready no vehicle": DISCONNECTED,
    "2": CHARGING,
    "charging": CHARGING,
    "laden": CHARGING,
    "3": PAUSED,
    "wait car": PAUSED,
    "waiting for vehicle": PAUSED,
    "4": FINISHED,
    "complete": FINISHED,
    "charge finished": FINISHED,
    "5": ERROR,
    "error": ERROR,
}

#: Per integration platform (`charger_profiles.PlatformProfile.platform`): its status sensor's values.
PLATFORM_STATUS: Final[dict[str, dict[str, str]]] = {
    "easee": {
        "disconnected": DISCONNECTED,
        "awaiting_start": CONNECTED,
        "awaiting_authorization": CONNECTED,
        "ready_to_charge": CONNECTED,
        "awaiting_scheduled_start": PAUSED,
        "awaiting_smart_start": PAUSED,
        "awaiting_load_balancing": PAUSED,
        "paused_due_to_equalizer": PAUSED,
        "charging": CHARGING,
        "completed": FINISHED,
        "error": ERROR,
    },
    "wallbox": {
        "disconnected": DISCONNECTED,
        "ready": CONNECTED,
        "locked, car connected": CONNECTED,
        "charging": CHARGING,
        "waiting for car demand": PAUSED,
        "paused": PAUSED,
        "scheduled": PAUSED,
        "waiting in queue by power sharing": PAUSED,
        "waiting in queue by power boost": PAUSED,
        "waiting in queue by eco-smart": PAUSED,
        "error": ERROR,
    },
    "zaptec": {
        "disconnected": DISCONNECTED,
        "connected_requesting": CONNECTED,
        "connected_charging": CHARGING,
        "connected_finished": FINISHED,
    },
    "goecharger_api2": _GOE,
    "goecharger_mqtt": _GOE,
    "goecharger": {
        "ready no vehicle": DISCONNECTED,
        "charging": CHARGING,
        "waiting for vehicle": PAUSED,
        "charge finished": FINISHED,
    },
    "wattpilot": {
        "idle": DISCONNECTED,
        "charging": CHARGING,
        "wait car": PAUSED,
        "complete": FINISHED,
        "error": ERROR,
    },
    "peblar": {
        "no_ev_connected": DISCONNECTED,
        "ev_connected": CONNECTED,
        "charging": CHARGING,
        "suspended": PAUSED,
        "error": ERROR,
        "fault": ERROR,
    },
    "nrgkick": {
        "standby": DISCONNECTED,
        "connected": CONNECTED,
        "charging": CHARGING,
        "error": ERROR,
    },
    "chargeamps": OCPP_STATUS,
    "lektrico": {
        "available": DISCONNECTED,
        "connected": CONNECTED,
        "need_auth": CONNECTED,
        "charging_candidate": CONNECTED,
        "charging": CHARGING,
        "paused": PAUSED,
        "paused_by_scheduler": PAUSED,
        "error": ERROR,
    },
    "openevse": {
        "not connected": DISCONNECTED,
        "not_connected": DISCONNECTED,
        "connected": CONNECTED,
        "charging": CHARGING,
        "sleeping": PAUSED,
        "disabled": PAUSED,
        "error": ERROR,
    },
    "alfen_modbus": {
        "a": DISCONNECTED,
        "b1": CONNECTED,
        "b2": CONNECTED,
        "c1": PAUSED,
        "c2": CHARGING,
        "d1": PAUSED,
        "d2": CHARGING,
        "e": ERROR,
        "f": ERROR,
    },
    "heidelberg_energy_control": {
        "a": DISCONNECTED,
        "a1": DISCONNECTED,
        "a2": DISCONNECTED,
        "b": CONNECTED,
        "b1": CONNECTED,
        "b2": CONNECTED,
        "c": CHARGING,
        "c2": CHARGING,
        "e": ERROR,
        "f": ERROR,
    },
    "garo_wallbox": {
        "disconnected": DISCONNECTED,
        "not_connected": DISCONNECTED,
        "connected": CONNECTED,
        "charging": CHARGING,
        "charging_paused": PAUSED,
        "charging_finished": FINISHED,
        "error": ERROR,
    },
    "smaev": {
        "active_mode": CHARGING,
        "sleep_mode": PAUSED,
    },
    "smartevse": {
        "ready to charge": DISCONNECTED,
        "connected to ev": CONNECTED,
        "charging": CHARGING,
        "error": ERROR,
    },
    "defa_power": {
        "disconnected": DISCONNECTED,
        "ev_connected": CONNECTED,
        "charging": CHARGING,
        "suspended_ev": PAUSED,
        "error": ERROR,
    },
    "webasto_next_modbus": {
        "charging": CHARGING,
        "error": ERROR,
    },
    "abb_terra_ac": {
        "state a - idle": DISCONNECTED,
        "state b1 - ev plug in, pending authorization": CONNECTED,
        "state b2 - ev plug in, charging complete": FINISHED,
        "state c1 - ev ready for charge": PAUSED,
        "state c2 - charging": CHARGING,
        "state e - error": ERROR,
        "state f - fault": ERROR,
    },
    "ohme": {
        "unplugged": DISCONNECTED,
        "plugged_in": CONNECTED,
        "pending_approval": CONNECTED,
        "charging": CHARGING,
        "paused": PAUSED,
        "finished": FINISHED,
    },
    "monta": {
        "available": DISCONNECTED,
        "busy-charging": CHARGING,
        "busy-non-charging": PAUSED,
        "busy-scheduled": PAUSED,
        "error": ERROR,
    },
}


def normalise_status(platform: str | None, raw: str | None) -> str:
    """The connection state a platform's raw status value means; `unknown` for anything not listed."""
    if raw is None:
        return UNKNOWN
    table = PLATFORM_STATUS.get(platform or "")
    if table is None:
        return UNKNOWN
    return table.get(raw.strip().lower(), UNKNOWN)


def normalise_ocpp(raw: str | None) -> str:
    """The connection state an OCPP connector status means; `unknown` for anything not listed."""
    if raw is None:
        return UNKNOWN
    return OCPP_STATUS.get(raw.strip().lower(), UNKNOWN)
