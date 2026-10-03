"""The charger's connection state: the mapping tables, unknown values, the adapter and the dashboard field."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.dashboard import serialize_connection
from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CHARGER_PLATFORM,
    CONF_CHARGING_STATE,
    CONF_MODE,
    MODE_OCPP,
)
from custom_components.spotnav.execution.charger_connection import (
    CHARGING,
    CONNECTED,
    CONNECTION_STATES,
    DISCONNECTED,
    ERROR,
    FINISHED,
    normalise_ocpp,
    normalise_status,
    OCPP_STATUS,
    PAUSED,
    PLATFORM_STATUS,
    UNKNOWN,
)
from custom_components.spotnav.execution.charger_profiles import PROFILES
from custom_components.spotnav.execution.chargers.adapter import build_adapter
from custom_components.spotnav.vehicles.ocpp_identity import OcppConnectorTarget

#: The mapping each adapter implements, spelled out so a change of one is a visible change here.
EXPECTED: dict[str, dict[str, str]] = {
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
        "offline": UNKNOWN,
    },
    "wallbox": {
        "disconnected": DISCONNECTED,
        "ready": CONNECTED,
        "charging": CHARGING,
        "waiting for car demand": PAUSED,
        "paused": PAUSED,
        "scheduled": PAUSED,
        "waiting in queue by power sharing": PAUSED,
        "error": ERROR,
        "locked": UNKNOWN,
        "updating": UNKNOWN,
    },
    "zaptec": {
        "disconnected": DISCONNECTED,
        "connected_requesting": CONNECTED,
        "connected_charging": CHARGING,
        "connected_finished": FINISHED,
        "unknown": UNKNOWN,
    },
    "goecharger_api2": {
        "1": DISCONNECTED,
        "Idle": DISCONNECTED,
        "2": CHARGING,
        "Charging": CHARGING,
        "Laden": CHARGING,
        "3": PAUSED,
        "Wait Car": PAUSED,
        "4": FINISHED,
        "Complete": FINISHED,
        "5": ERROR,
        "0": UNKNOWN,
    },
    "wattpilot": {"Idle": DISCONNECTED, "Charging": CHARGING, "Wait Car": PAUSED, "Complete": FINISHED, "Error": ERROR},
    "peblar": {"no_ev_connected": DISCONNECTED, "ev_connected": CONNECTED, "charging": CHARGING, "suspended": PAUSED, "error": ERROR},
    "nrgkick": {"standby": DISCONNECTED, "connected": CONNECTED, "charging": CHARGING, "error": ERROR, "wakeup": UNKNOWN},
    "chargeamps": {
        "Available": DISCONNECTED,
        "Preparing": CONNECTED,
        "Charging": CHARGING,
        "SuspendedEV": PAUSED,
        "SuspendedEVSE": PAUSED,
        "Finishing": FINISHED,
        "Faulted": ERROR,
        "Unavailable": UNKNOWN,
    },
    "lektrico": {"available": DISCONNECTED, "connected": CONNECTED, "paused": PAUSED, "charging": CHARGING, "error": ERROR},
    "openevse": {"not connected": DISCONNECTED, "connected": CONNECTED, "charging": CHARGING, "sleeping": PAUSED, "error": ERROR},
    "alfen_modbus": {"A": DISCONNECTED, "B2": CONNECTED, "C1": PAUSED, "C2": CHARGING, "E": ERROR},
    "garo_wallbox": {"charging": CHARGING, "charging_paused": PAUSED, "charging_finished": FINISHED, "connected": CONNECTED},
    "ohme": {"unplugged": DISCONNECTED, "plugged_in": CONNECTED, "charging": CHARGING, "paused": PAUSED, "finished": FINISHED},
    "abb_terra_ac": {
        "State A - Idle": DISCONNECTED,
        "State C2 - Charging": CHARGING,
        "State B2 - EV plug in, charging complete": FINISHED,
        "State F - Fault": ERROR,
    },
    "monta": {"available": DISCONNECTED, "busy-charging": CHARGING, "busy-non-charging": PAUSED, "error": ERROR},
}

OCPP_EXPECTED = {
    "Available": DISCONNECTED,
    "Preparing": CONNECTED,
    "Charging": CHARGING,
    "SuspendedEV": PAUSED,
    "SuspendedEVSE": PAUSED,
    "Finishing": FINISHED,
    "Faulted": ERROR,
    "Unavailable": UNKNOWN,
    "Reserved": UNKNOWN,
}


@pytest.mark.parametrize(
    ("platform", "raw", "expected"),
    [(platform, raw, state) for platform, rows in EXPECTED.items() for raw, state in rows.items()],
)
def test_every_adapter_maps_its_raw_values(platform: str, raw: str, expected: str) -> None:
    assert normalise_status(platform, raw) == expected


@pytest.mark.parametrize(("raw", "expected"), list(OCPP_EXPECTED.items()))
def test_ocpp_connector_statuses(raw: str, expected: str) -> None:
    assert normalise_ocpp(raw) == expected


def test_unmapped_missing_and_platformless_values_are_unknown() -> None:
    assert normalise_status("easee", "something_new") == UNKNOWN
    assert normalise_status("easee", None) == UNKNOWN
    assert normalise_status("v2c", "charging") == UNKNOWN
    assert normalise_status(None, "charging") == UNKNOWN
    assert normalise_ocpp("Frobnicating") == UNKNOWN
    assert normalise_ocpp(None) == UNKNOWN


def test_the_tables_only_hold_known_states_and_lower_case_keys() -> None:
    for platform, table in PLATFORM_STATUS.items():
        assert platform in PROFILES, platform
        for raw, state in table.items():
            assert raw == raw.lower(), (platform, raw)
            assert state in CONNECTION_STATES and state != UNKNOWN, (platform, raw)
    assert set(OCPP_STATUS.values()) <= set(CONNECTION_STATES)


def test_the_tables_agree_with_what_each_profile_already_says() -> None:
    """Charging values chart as charging, idle ones as a connected vehicle, disconnected ones as none."""
    for platform, profile in PROFILES.items():
        if not profile.status_keys:
            continue
        for value in profile.charging_values:
            assert normalise_status(platform, value) == CHARGING, (platform, value)
        for value in profile.vehicle_idle_values:
            assert normalise_status(platform, value) not in (DISCONNECTED, UNKNOWN, CHARGING), (platform, value)
        for value in profile.disconnected_values:
            assert normalise_status(platform, value) == DISCONNECTED, (platform, value)


def _config(platform: str | None, *, status: str | None, charging_values: list[str] | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {}
    if status is not None:
        state = {"entity_id": status, "charging_values": charging_values or ["charging"]}
    return {CONF_CHARGER_PLATFORM: platform, CONF_CHARGE_CONTROL: "switch.charger", CONF_CHARGING_STATE: state}


def _adapter(hass: HomeAssistant, config: dict[str, Any], target: OcppConnectorTarget | None = None):
    return build_adapter(hass, config, ocpp_target=lambda: target, energy_entity_id=None)


async def test_a_status_sensor_is_mapped_by_the_platform_and_named_as_the_source(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _config("easee", status="sensor.easee_status"))
    for raw, expected in EXPECTED["easee"].items():
        hass.states.async_set("sensor.easee_status", raw)
        assert adapter.connection() == (expected, "sensor.easee_status")


async def test_an_unreadable_status_is_unknown_with_its_source(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _config("easee", status="sensor.easee_status"))
    assert adapter.connection() == (UNKNOWN, "sensor.easee_status")
    hass.states.async_set("sensor.easee_status", "unavailable")
    assert adapter.connection() == (UNKNOWN, "sensor.easee_status")


async def test_an_unlisted_status_is_unknown_unless_the_chosen_charging_values_say_charging(
    hass: HomeAssistant,
) -> None:
    adapter = _adapter(hass, _config("easee", status="sensor.s", charging_values=["charging", "boosting"]))
    hass.states.async_set("sensor.s", "mystery")
    assert adapter.connection() == (UNKNOWN, "sensor.s")
    hass.states.async_set("sensor.s", "boosting")
    assert adapter.connection() == (CHARGING, "sensor.s")
    other = _adapter(hass, _config("some_new_platform", status="sensor.s", charging_values=["go"]))
    hass.states.async_set("sensor.s", "go")
    assert other.connection() == (CHARGING, "sensor.s")
    hass.states.async_set("sensor.s", "idle")
    assert other.connection() == (UNKNOWN, "sensor.s")


async def test_a_switch_only_charger_is_charging_or_unknown(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _config(None, status=None))
    assert adapter.connection() == (UNKNOWN, None)
    hass.states.async_set("switch.charger", "off")
    assert adapter.connection() == (UNKNOWN, None)
    hass.states.async_set("switch.charger", "on")
    assert adapter.connection() == (CHARGING, None)


async def test_an_ocpp_connector_status_is_read_from_the_connector(hass: HomeAssistant) -> None:
    config = _config(None, status=None)
    config[CONF_MODE] = MODE_OCPP
    target = OcppConnectorTarget(charge_point_id="cp1", connector_id=1)
    adapter = _adapter(hass, config, target)
    entity = "sensor.cp1_connector_1_status_connector"
    for raw, expected in OCPP_EXPECTED.items():
        hass.states.async_set(entity, raw)
        assert adapter.connection() == (expected, entity)
    hass.states.async_set(entity, "unavailable")
    assert adapter.connection() == (UNKNOWN, None)


def test_the_dashboard_field() -> None:
    assert serialize_connection((CHARGING, "sensor.s")) == {"state": "charging", "source": "sensor.s"}
    assert serialize_connection((UNKNOWN, None)) == {"state": "unknown", "source": None}
    assert serialize_connection(("bogus", "")) == {"state": "unknown", "source": None}
