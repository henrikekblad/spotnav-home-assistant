"""Recorded entity shapes of the chargers SpotNav detects and drives.

Each shape is what an integration registers for one charger, as read from
the integration's source: the platform, the unique id (and translation key where the integration has
one), the domain, the unit and classes, and a typical state. Tests register them in the real entity
and device registries and run detection and the adapters against them -- never against a live
integration. `expect` is what detection must find.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from pytest_homeassistant_custom_component.common import MockConfigEntry

ENERGY_KWH = {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"}
ENERGY_WH = {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "Wh"}
AMPS = {"device_class": "current", "unit_of_measurement": "A"}
MILLIAMPS = {"device_class": "current", "unit_of_measurement": "mA"}
NUMBER_A = {"unit_of_measurement": "A", "min": 6, "max": 16, "step": 1}


@dataclass(frozen=True)
class E:
    """One registered entity: its unique id, key and state, and whether it is registered disabled."""

    domain: str
    unique_id: str
    key: str
    state: str = "unknown"
    attrs: dict[str, Any] = field(default_factory=dict)
    #: The integration sets a translation key (core integrations); custom ones rely on the unique id.
    translation_key: bool = False
    disabled: bool = False


@dataclass(frozen=True)
class Shape:
    platform: str
    serial: str
    entities: tuple[E, ...]
    expect: dict[str, Any]
    #: A parent device (Zaptec's installation) and what to register on it.
    installation: tuple[E, ...] = ()


def _e(domain: str, unique_id: str, key: str, state: str = "unknown", attrs: dict | None = None, **kw: Any) -> E:
    return E(domain, unique_id, key, state, attrs or {}, **kw)


SHAPES: dict[str, Shape] = {
    "wallbox": Shape(
        "wallbox",
        "WB123",
        (
            _e("switch", "pause_resume-WB123", "pause_resume", "on", translation_key=True),
            _e("number", "max_charging_current-WB123", "maximum_charging_current", "16", NUMBER_A, translation_key=True),
            _e("sensor", "added_energy-WB123", "added_energy", "4.2", ENERGY_KWH, translation_key=True),
            _e("sensor", "status_description-WB123", "status_description", "Charging", translation_key=True),
            _e("select", "ecosmart-WB123", "ecosmart", "off", {"options": ["off", "eco_mode", "full_solar"]}, translation_key=True),
        ),
        {
            "charge_control": "switch.wallbox_pause_resume",
            "path": {"kind": "switch", "inverted": False},
            "current_limit": "number.wallbox_maximum_charging_current",
            "current_control": "number",
            "energy_register": None,
            "session_energy_register": "sensor.wallbox_added_energy",
            "charging_state": "sensor.wallbox_status_description",
            "current_entities": [],
        },
    ),
    "goecharger_api2": Shape(
        "goecharger_api2",
        "123456",
        (
            _e("select", "goecharger_api2.goe_123456_frc", "frc", "Neutral", {"options": ["Neutral", "Don't charge", "Charge"]}),
            _e("number", "goecharger_api2.goe_123456_amp", "amp", "16", NUMBER_A),
            _e("sensor", "goecharger_api2.goe_123456_eto", "eto", "1234500", ENERGY_WH),
            _e("sensor", "goecharger_api2.goe_123456_car", "car", "Charging"),
            _e("sensor", "goecharger_api2.goe_123456_nrg_4", "nrg_4", "10.1", AMPS),
            _e("sensor", "goecharger_api2.goe_123456_nrg_5", "nrg_5", "10.0", AMPS),
            _e("sensor", "goecharger_api2.goe_123456_nrg_6", "nrg_6", "9.9", AMPS),
            _e("select", "goecharger_api2.goe_123456_lmo", "lmo", "3", {"options": ["3", "4", "5"]}),
            _e("switch", "goecharger_api2.goe_123456_fup", "fup", "off"),
        ),
        {
            "charge_control": "select.goecharger_api2_frc",
            "path": {"kind": "select", "start_option": "Charge", "stop_option": "Don't charge"},
            "current_limit": "number.goecharger_api2_amp",
            "current_control": "number",
            "energy_register": "sensor.goecharger_api2_eto",
            "session_energy_register": None,
            "charging_state": "sensor.goecharger_api2_car",
            "current_entities": [
                "sensor.goecharger_api2_nrg_4",
                "sensor.goecharger_api2_nrg_5",
                "sensor.goecharger_api2_nrg_6",
            ],
        },
    ),
    "peblar": Shape(
        "peblar",
        "PB777",
        (
            _e("switch", "PB777_charge", "charge", "on", translation_key=True),
            _e("number", "PB777_charge_current_limit", "charge_current_limit", "16", NUMBER_A, translation_key=True),
            _e("sensor", "PB777_energy_total", "energy_total", "88000", ENERGY_WH, translation_key=True),
            _e("sensor", "PB777_energy_session", "energy_session", "4000", ENERGY_WH, translation_key=True),
            _e("sensor", "PB777_cp_state", "cp_state", "charging", translation_key=True),
            _e("sensor", "PB777_current_phase_1", "current_phase_1", "9800", MILLIAMPS, translation_key=True, disabled=True),
            _e("sensor", "PB777_current_phase_2", "current_phase_2", "9900", MILLIAMPS, translation_key=True, disabled=True),
            _e("sensor", "PB777_current_phase_3", "current_phase_3", "10000", MILLIAMPS, translation_key=True, disabled=True),
            _e("select", "PB777_smart_charging", "smart_charging", "default", {"options": ["default", "pure_solar"]}, translation_key=True),
        ),
        {
            "charge_control": "switch.peblar_charge",
            "path": {"kind": "switch_budget", "inverted": False},
            "current_limit": "number.peblar_charge_current_limit",
            "current_control": "number",
            "energy_register": "sensor.peblar_energy_total",
            "session_energy_register": None,
            "charging_state": "sensor.peblar_cp_state",
            "current_entities": [
                "sensor.peblar_current_phase_1",
                "sensor.peblar_current_phase_2",
                "sensor.peblar_current_phase_3",
            ],
            "disabled_useful": [
                "sensor.peblar_current_phase_1",
                "sensor.peblar_current_phase_2",
                "sensor.peblar_current_phase_3",
            ],
        },
    ),
    "nrgkick": Shape(
        "nrgkick",
        "NK42",
        (
            _e("switch", "NK42_charging_enabled", "charging_enabled", "on", translation_key=True),
            _e("number", "NK42_current_set", "current_set", "12.0", {**NUMBER_A, "step": 0.1}, translation_key=True),
            _e("sensor", "NK42_total_charged_energy", "total_charged_energy", "512000", ENERGY_WH, translation_key=True),
            _e("sensor", "NK42_status", "status", "charging", translation_key=True),
            _e("sensor", "NK42_l1_current", "l1_current", "11.9", AMPS, translation_key=True),
            _e("sensor", "NK42_l2_current", "l2_current", "12.0", AMPS, translation_key=True),
            _e("sensor", "NK42_l3_current", "l3_current", "12.1", AMPS, translation_key=True),
        ),
        {
            "charge_control": "switch.nrgkick_charging_enabled",
            "path": {"kind": "switch", "inverted": False},
            "current_limit": "number.nrgkick_current_set",
            "current_control": "number",
            "energy_register": "sensor.nrgkick_total_charged_energy",
            "session_energy_register": None,
            "charging_state": "sensor.nrgkick_status",
            "current_entities": [
                "sensor.nrgkick_l1_current",
                "sensor.nrgkick_l2_current",
                "sensor.nrgkick_l3_current",
            ],
        },
    ),
    "hypervolt_charger": Shape(
        "hypervolt_charger",
        "HV1",
        (
            _e("switch", "HV1_charging", "charging", "on"),
            _e("number", "HV1_max_current", "max_current", "32", {**NUMBER_A, "max": 32}),
            _e(
                "select",
                "HV1_schedule_session_1_charge_mode",
                "schedule_session_1_charge_mode",
                "Eco",
                {"options": ["Boost", "Eco", "Super Eco", "Battery Safe"]},
            ),
            _e(
                "select",
                "HV1_charge_mode",
                "charge_mode",
                "Super Eco",
                {"options": ["Boost", "Eco", "Super Eco", "Battery Safe"]},
            ),
            _e(
                "select",
                "HV1_activation_mode",
                "activation_mode",
                "Schedule",
                {"options": ["Plug and Charge", "Schedule", "Octopus"]},
            ),
        ),
        {
            "charge_control": "switch.hypervolt_charger_charging",
            "path": {"kind": "switch", "inverted": False},
            "current_limit": "number.hypervolt_charger_max_current",
            "current_control": "number",
            "energy_register": None,
            "session_energy_register": None,
            "charging_state": None,
            "current_entities": [],
            "conflicts": [
                "select.hypervolt_charger_activation_mode",
                "select.hypervolt_charger_charge_mode",
            ],
        },
    ),
    "chargeamps": Shape(
        "chargeamps",
        "CA9",
        (
            _e("switch", "chargeamps_CA9_1_enable", "enable", "on"),
            _e("number", "chargeamps_CA9_1_max_current", "max_current", "16", NUMBER_A),
            _e("sensor", "chargeamps_CA9_1_total_energy", "total_energy", "301.5", ENERGY_KWH),
            _e("sensor", "chargeamps_CA9_1_status", "status", "Charging"),
            _e("sensor", "chargeamps_CA9_1_l1_current", "l1_current", "10", AMPS),
            _e("sensor", "chargeamps_CA9_1_l2_current", "l2_current", "10", AMPS),
            _e("sensor", "chargeamps_CA9_1_l3_current", "l3_current", "10", AMPS),
            _e("switch", "chargeamps_CA9_1_schedule", "schedule", "on"),
        ),
        {
            "charge_control": "switch.chargeamps_enable",
            "path": {"kind": "switch", "inverted": False},
            "current_limit": "number.chargeamps_max_current",
            "current_control": "number",
            "energy_register": "sensor.chargeamps_total_energy",
            "session_energy_register": None,
            "charging_state": "sensor.chargeamps_status",
            "current_entities": [
                "sensor.chargeamps_l1_current",
                "sensor.chargeamps_l2_current",
                "sensor.chargeamps_l3_current",
            ],
            "conflicts": ["switch.chargeamps_schedule"],
        },
    ),
    "monta": Shape(
        "monta",
        "M55",
        (
            _e("switch", "ENTRY_M55_charger", "charger", "on"),
            _e("sensor", "ENTRY_M55_charger_state", "charger_state", "busy-charging"),
            _e("sensor", "ENTRY_M55_charger_lastmeterreadingkwh", "charger_lastmeterreadingkwh", "120.4", ENERGY_KWH),
        ),
        {
            "charge_control": "switch.monta_charger",
            "path": {"kind": "switch", "inverted": False},
            "current_limit": None,
            "current_control": "",
            "energy_register": "sensor.monta_charger_lastmeterreadingkwh",
            "session_energy_register": None,
            "charging_state": "sensor.monta_charger_state",
            "current_entities": [],
        },
    ),
    "zaptec": Shape(
        "zaptec",
        "ZAP1",
        (
            _e("switch", "ZAP1_charger_operation_mode", "charger_operation_mode", "on"),
            _e("sensor", "ZAP1_charger_operation_mode", "charger_operation_mode", "connected_charging"),
            _e("sensor", "ZAP1_signed_meter_value_kwh", "signed_meter_value_kwh", "900.2", ENERGY_KWH),
            _e("sensor", "ZAP1_current_phase1", "current_phase1", "10", AMPS),
            _e("sensor", "ZAP1_current_phase2", "current_phase2", "10", AMPS),
            _e("sensor", "ZAP1_current_phase3", "current_phase3", "10", AMPS),
        ),
        {
            "charge_control": "switch.zaptec_charger_operation_mode",
            "path": {"kind": "switch", "inverted": False},
            "current_limit": "number.zaptec_available_current",
            "current_control": "number",
            "energy_register": "sensor.zaptec_signed_meter_value_kwh",
            "session_energy_register": None,
            "charging_state": "sensor.zaptec_charger_operation_mode",
            "current_entities": [
                "sensor.zaptec_current_phase1",
                "sensor.zaptec_current_phase2",
                "sensor.zaptec_current_phase3",
            ],
        },
        installation=(_e("number", "INST_available_current", "available_current", "16", NUMBER_A),),
    ),
    "easee": Shape(
        "easee",
        "EH123",
        (
            _e("sensor", "EH123_status", "status", "charging", {"config_authorizationRequired": False}),
            _e("sensor", "EH123_lifetime_energy", "lifetime_energy", "2450.3", ENERGY_KWH),
            _e("sensor", "EH123_session_energy", "session_energy", "8.1", ENERGY_KWH),
            _e("sensor", "EH123_current", "current", "9.9", AMPS, disabled=True),
            _e("sensor", "EH123_dynamic_charger_limit", "dynamic_charger_limit", "16", AMPS, disabled=True),
            _e("switch", "EH123_is_enabled", "is_enabled", "on"),
        ),
        {
            "charge_control": "sensor.easee_status",
            "path": {"kind": "easee"},
            "current_limit": None,
            "current_control": "easee_dynamic_limit",
            "energy_register": "sensor.easee_lifetime_energy",
            "session_energy_register": None,
            "charging_state": "sensor.easee_status",
            "current_entities": ["sensor.easee_current"],
            "disabled_useful": ["sensor.easee_current", "sensor.easee_dynamic_charger_limit"],
        },
    ),
    "v2c": Shape(
        "v2c",
        "V2C1",
        (
            _e("switch", "V2C1_paused", "paused", "off", translation_key=True),
            _e("number", "V2C1_intensity", "intensity", "16", NUMBER_A, translation_key=True),
            _e("sensor", "V2C1_charge_energy", "charge_energy", "3.3", ENERGY_KWH, translation_key=True),
            _e("switch", "V2C1_dynamic", "dynamic", "off", translation_key=True),
        ),
        {
            "charge_control": "switch.v2c_paused",
            "path": {"kind": "switch", "inverted": True},
            "current_limit": "number.v2c_intensity",
            "current_control": "number",
            "energy_register": None,
            "session_energy_register": "sensor.v2c_charge_energy",
            "charging_state": None,
            "current_entities": [],
        },
    ),
    "myenergi": Shape(
        "myenergi",
        "ZAPPI1",
        (_e("select", "ENTRY-ZAPPI1-charge_mode", "charge_mode", "Stopped", {"options": ["Fast", "Eco", "Eco+", "Stopped"]}),),
        {
            "charge_control": "select.myenergi_charge_mode",
            "path": {"kind": "select", "start_option": "Fast", "stop_option": "Stopped"},
            "current_limit": None,
            "current_control": "",
            "energy_register": None,
            "session_energy_register": None,
            "charging_state": None,
            "current_entities": [],
        },
    ),
    "lektrico": Shape(
        "lektrico",
        "LK1",
        (
            _e("button", "LK1_charge_start", "charge_start", translation_key=True),
            _e("button", "LK1_charge_stop", "charge_stop", translation_key=True),
            _e("number", "LK1_dynamic_limit", "dynamic_limit", "16", NUMBER_A, translation_key=True),
            _e("sensor", "LK1_lifetime_energy", "lifetime_energy", "77.7", ENERGY_KWH, translation_key=True),
            _e("sensor", "LK1_state", "state", "charging", translation_key=True),
        ),
        {
            "charge_control": "button.lektrico_charge_start",
            "path": {
                "kind": "buttons",
                "start_entity_id": "button.lektrico_charge_start",
                "stop_entity_id": "button.lektrico_charge_stop",
            },
            "current_limit": "number.lektrico_dynamic_limit",
            "current_control": "number",
            "energy_register": "sensor.lektrico_lifetime_energy",
            "session_energy_register": None,
            "charging_state": "sensor.lektrico_state",
            "current_entities": [],
        },
    ),
    "openevse": Shape(
        "openevse",
        "OE1",
        (
            _e("select", "OE1-override_state", "override_state", "auto", {"options": ["auto", "active", "disabled"]}, translation_key=True),
            _e("number", "OE1-charge_rate", "charge_rate", "32", {**NUMBER_A, "max": 32}, translation_key=True),
            _e("sensor", "OE1-status", "status", "charging", {"options": ["charging", "connected", "not_connected", "sleeping", "disabled"]}, translation_key=True),
            _e("sensor", "OE1-usage_total", "usage_total", "310.0", ENERGY_KWH, translation_key=True),
            _e("sensor", "OE1-usage_session", "usage_session", "4.1", ENERGY_KWH, translation_key=True),
            _e("switch", "OE1-solar_pv_divert", "solar_pv_divert", "on", translation_key=True),
        ),
        {
            "charge_control": "select.openevse_override_state",
            "path": {"kind": "select", "start_option": "active", "stop_option": "disabled"},
            "current_limit": "number.openevse_charge_rate",
            "current_control": "number",
            "energy_register": "sensor.openevse_usage_total",
            "session_energy_register": None,
            "charging_state": "sensor.openevse_status",
            "current_entities": [],
            "conflicts": ["switch.openevse_solar_pv_divert"],
        },
    ),
    "alfen_wallbox": Shape(
        "alfen_wallbox",
        "AL1",
        (
            _e("number", "AL1_main_normal_max_current_socket_1", "main_normal_max_current_socket_1", "16", NUMBER_A),
        ),
        {
            "charge_control": None,
            "path": None,
            "current_limit": "number.alfen_wallbox_main_normal_max_current_socket_1",
            "current_control": "number",
            "energy_register": None,
            "session_energy_register": None,
            "charging_state": None,
            "current_entities": [],
        },
    ),
    "goecharger_mqtt": Shape(
        "goecharger_mqtt",
        "654321",
        (
            _e("select", "654321-select-frc-0", "frc", "neutral", {"options": ["neutral", "dont_charge", "charge"]}, translation_key=True),
            _e("number", "654321-number-amp-0", "amp", "16", NUMBER_A, translation_key=True),
            _e("sensor", "654321-sensor-eto-0", "eto", "1234500", ENERGY_WH, translation_key=True),
            _e("sensor", "654321-sensor-car-0", "car", "charging", translation_key=True),
            _e("sensor", "654321-sensor-nrg_4-0", "nrg_4", "10.1", AMPS, translation_key=True),
            _e("sensor", "654321-sensor-nrg_5-0", "nrg_5", "10.0", AMPS, translation_key=True),
            _e("sensor", "654321-sensor-nrg_6-0", "nrg_6", "9.9", AMPS, translation_key=True),
            _e("select", "654321-select-lmo-0", "lmo", "default", {"options": ["default", "awattar", "auto_stop"]}, translation_key=True),
        ),
        {
            "charge_control": "select.goecharger_mqtt_frc",
            "path": {"kind": "select", "start_option": "charge", "stop_option": "dont_charge"},
            "current_limit": "number.goecharger_mqtt_amp",
            "current_control": "number",
            "energy_register": "sensor.goecharger_mqtt_eto",
            "session_energy_register": None,
            "charging_state": "sensor.goecharger_mqtt_car",
            "current_entities": [
                "sensor.goecharger_mqtt_nrg_4",
                "sensor.goecharger_mqtt_nrg_5",
                "sensor.goecharger_mqtt_nrg_6",
            ],
        },
    ),
    "goecharger": Shape(
        "goecharger",
        "garage",
        (
            _e("switch", "garage_allow_charging", "allow_charging", "on"),
            _e("sensor", "garage_car_status", "car_status", "Charger ready, no vehicle"),
        ),
        {
            "charge_control": "switch.goecharger_allow_charging",
            "path": {"kind": "switch", "inverted": False},
            "current_limit": None,
            "current_control": "",
            "energy_register": None,
            "session_energy_register": None,
            "charging_state": "sensor.goecharger_car_status",
            "current_entities": [],
        },
    ),
    "wattpilot": Shape(
        "wattpilot",
        "WP1",
        (
            _e("button", "WP1-frc2", "frc2"),
            _e("button", "WP1-frc1", "frc1"),
            _e("button", "WP1-frc0", "frc0"),
            _e("number", "WP1-amp", "amp", "16", NUMBER_A),
            _e("sensor", "WP1-car", "car", "Wait Car"),
            _e("select", "WP1-lmo", "lmo", "Default", {"options": ["Default", "Eco", "Next Trip"]}),
        ),
        {
            "charge_control": "button.wattpilot_frc2",
            "path": {
                "kind": "buttons",
                "start_entity_id": "button.wattpilot_frc2",
                "stop_entity_id": "button.wattpilot_frc1",
            },
            "current_limit": "number.wattpilot_amp",
            "current_control": "number",
            "energy_register": None,
            "session_energy_register": None,
            "charging_state": "sensor.wattpilot_car",
            "current_entities": [],
        },
    ),
    "alfen_modbus": Shape(
        "alfen_modbus",
        "AM1",
        (
            _e("switch", "AM1_socket_1_chargerEnabled", "charger_enabled", "on", translation_key=True),
            _e("number", "AM1_maxCurrent_socket_1", "max_current_limit", "16", {**NUMBER_A, "min": 0, "step": 0.1}, translation_key=True),
            _e("sensor", "AM1_socket_1_mode3state", "mode_3_state", "C2", translation_key=True),
        ),
        {
            "charge_control": "switch.alfen_modbus_charger_enabled",
            "path": {"kind": "switch", "inverted": False},
            "current_limit": "number.alfen_modbus_max_current_limit",
            "current_control": "number",
            "energy_register": None,
            "session_energy_register": None,
            "charging_state": "sensor.alfen_modbus_mode_3_state",
            "current_entities": [],
        },
    ),
    "heidelberg_energy_control": Shape(
        "heidelberg_energy_control",
        "HD1",
        (
            _e("switch", "HD1_virtual_enable", "virtual_enable", "on", translation_key=True),
            _e("number", "HD1_virtual_current", "virtual_current", "16", {**NUMBER_A, "step": 0.1}, translation_key=True),
            _e("sensor", "HD1_charging_state", "charging_state", "C", translation_key=True),
            _e("sensor", "HD1_total_energy", "total_energy", "812.4", ENERGY_KWH, translation_key=True),
        ),
        {
            "charge_control": "switch.heidelberg_energy_control_virtual_enable",
            "path": {"kind": "switch", "inverted": False},
            "current_limit": "number.heidelberg_energy_control_virtual_current",
            "current_control": "number",
            "energy_register": "sensor.heidelberg_energy_control_total_energy",
            "session_energy_register": None,
            "charging_state": "sensor.heidelberg_energy_control_charging_state",
            "current_entities": [],
        },
    ),
    "garo_wallbox": Shape(
        "garo_wallbox",
        "GA1",
        (
            _e("select", "GA1-sensor", "sensor", "ALWAYS_ON", {"options": ["ALWAYS_ON", "ALWAYS_OFF", "SCHEMA"]}, translation_key=True),
            _e("sensor", "GA1-sensor", "sensor", "ALWAYS_ON", {"options": ["ALWAYS_ON", "ALWAYS_OFF", "SCHEMA"]}, translation_key=True),
            _e("number", "GA1-current_limit", "current_limit", "16", NUMBER_A, translation_key=True),
            _e("sensor", "GA1-status", "status", "CHARGING_PAUSED", translation_key=True),
            _e("sensor", "GA1-acc_energy", "acc_energy", "1530.2", ENERGY_KWH, translation_key=True),
            _e("sensor", "GA1-acc_session_energy", "acc_session_energy", "6100", ENERGY_WH, translation_key=True),
        ),
        {
            "charge_control": "select.garo_wallbox_sensor",
            "path": {"kind": "select", "start_option": "ALWAYS_ON", "stop_option": "ALWAYS_OFF"},
            "current_limit": "number.garo_wallbox_current_limit",
            "current_control": "number",
            "energy_register": "sensor.garo_wallbox_acc_energy",
            "session_energy_register": None,
            "charging_state": "sensor.garo_wallbox_status",
            "current_entities": [],
        },
    ),
    "smaev": Shape(
        "smaev",
        "SMA1",
        (
            _e(
                "select",
                "SMA1-operating_mode_of_charge_session",
                "operating_mode_of_charge_session",
                "optimized_charging",
                {"options": ["boost_charging", "optimized_charging", "setpoint_charging", "charge_stop"]},
                translation_key=True,
            ),
            _e("number", "SMA1-charge_current_limit", "charge_current_limit", "16", {**NUMBER_A, "step": 0.001}, translation_key=True, disabled=True),
            _e("sensor", "SMA1-charging_session_energy", "charging_session_energy", "5200", ENERGY_WH, translation_key=True),
            _e("sensor", "SMA1-charging_station_meter_reading", "charging_station_meter_reading", "910000", ENERGY_WH, translation_key=True),
            _e("sensor", "SMA1-charging_session_status", "charging_session_status", "sleep_mode", translation_key=True),
        ),
        {
            "charge_control": "select.smaev_operating_mode_of_charge_session",
            "path": {"kind": "select", "start_option": "boost_charging", "stop_option": "charge_stop"},
            "current_limit": "number.smaev_charge_current_limit",
            "current_control": "number",
            "energy_register": "sensor.smaev_charging_station_meter_reading",
            "session_energy_register": None,
            "charging_state": "sensor.smaev_charging_session_status",
            "current_entities": [],
            "disabled_useful": ["number.smaev_charge_current_limit"],
        },
    ),
    "smartevse": Shape(
        "smartevse",
        "SE1",
        (
            _e("select", "SE1_smartevse_mode_id", "smartevse_mode_id", "SMART", {"options": ["OFF", "NORMAL", "SOLAR", "SMART", "PAUSE"]}),
            _e("sensor", "SE1_smartevse_mode_id", "smartevse_mode_id", "3"),
            _e("number", "SE1_smartevse_override_current", "smartevse_override_current", "0", {**NUMBER_A, "step": 0.1}),
            _e("sensor", "SE1_smartevse_state", "smartevse_state", "Connected to EV"),
            _e("sensor", "SE1_smartevse_ev_total_kwh", "smartevse_ev_total_kwh", "540.3", ENERGY_KWH),
            _e("sensor", "SE1_smartevse_ev_charged_kwh", "smartevse_ev_charged_kwh", "9.1", {**ENERGY_KWH, "state_class": "total"}),
        ),
        {
            "charge_control": "select.smartevse_smartevse_mode_id",
            "path": {"kind": "select_restore", "start_option": "NORMAL", "stop_option": "PAUSE"},
            "current_limit": "number.smartevse_smartevse_override_current",
            "current_control": "number",
            "energy_register": "sensor.smartevse_smartevse_ev_total_kwh",
            "session_energy_register": None,
            "charging_state": "sensor.smartevse_smartevse_state",
            "current_entities": [],
        },
    ),
    "defa_power": Shape(
        "defa_power",
        "DF1",
        (
            _e("button", "DF1_c1_start_charging", "start_charging"),
            _e("button", "DF1_c1_stop_charging", "stop_charging"),
            _e("number", "DF1_c1_ampere", "ampere", "16", NUMBER_A),
            _e("sensor", "DF1_c1_transaction_meter_value", "transaction_meter_value", "7.2", {**ENERGY_KWH, "state_class": "total"}),
            _e("sensor", "DF1_c1_meter_value", "meter_value", "2210.5", {**ENERGY_KWH, "state_class": "total"}),
            _e("sensor", "DF1_c1_charging_state", "charging_state", "suspended_evse"),
            _e("switch", "DF1_c1_eco_mode_active", "eco_mode_active", "on"),
        ),
        {
            "charge_control": "button.defa_power_start_charging",
            "path": {
                "kind": "buttons",
                "start_entity_id": "button.defa_power_start_charging",
                "stop_entity_id": "button.defa_power_stop_charging",
            },
            "current_limit": "number.defa_power_ampere",
            "current_control": "number",
            "energy_register": "sensor.defa_power_meter_value",
            "session_energy_register": None,
            "charging_state": "sensor.defa_power_charging_state",
            "current_entities": [],
            "conflicts": ["switch.defa_power_eco_mode_active"],
        },
    ),
    "webasto_next_modbus": Shape(
        "webasto_next_modbus",
        "WN1",
        (
            _e("button", "WN1-start_session", "start_session", translation_key=True),
            _e("button", "WN1-stop_session", "stop_session", translation_key=True),
            _e("number", "WN1-set_current_a", "set_current_a", "16", {**NUMBER_A, "min": 0, "max": 32}, translation_key=True),
            _e("sensor", "WN1-charge_point_state", "charge_point_state", "preparing", translation_key=True),
            _e("sensor", "WN1-charging_state", "charging_state", "idle", translation_key=True),
        ),
        {
            "charge_control": "button.webasto_next_modbus_start_session",
            "path": {
                "kind": "buttons_toggle",
                "start_entity_id": "button.webasto_next_modbus_start_session",
                "stop_entity_id": "button.webasto_next_modbus_stop_session",
            },
            "current_limit": "number.webasto_next_modbus_set_current_a",
            "current_control": "number",
            "energy_register": None,
            "session_energy_register": None,
            "charging_state": "sensor.webasto_next_modbus_charging_state",
            "current_entities": [],
        },
    ),
    "abb_terra_ac": Shape(
        "abb_terra_ac",
        "ABB1",
        (
            _e("button", "ABB1_start_charging", "start_charging", translation_key=True),
            _e("button", "ABB1_stop_charging", "stop_charging", translation_key=True),
            _e("number", "ABB1_current_limit", "current_limit", "16", {**NUMBER_A, "min": 0, "max": 32}, translation_key=True),
            _e("sensor", "ABB1_charging_state", "charging_state", "State C2 - Charging", translation_key=True),
            _e("sensor", "ABB1_energy_delivered", "energy_delivered", "12.5", {**ENERGY_KWH, "state_class": "total"}, translation_key=True),
        ),
        {
            "charge_control": "button.abb_terra_ac_start_charging",
            "path": {
                "kind": "number_pause",
                "start_entity_id": "button.abb_terra_ac_start_charging",
                "number_entity_id": "number.abb_terra_ac_current_limit",
            },
            "current_limit": "number.abb_terra_ac_current_limit",
            "current_control": "number",
            "energy_register": None,
            "session_energy_register": None,
            "charging_state": "sensor.abb_terra_ac_charging_state",
            "current_entities": [],
        },
    ),
    "ohme": Shape(
        "ohme",
        "OH1",
        (
            _e("select", "OH1_charge_mode", "charge_mode", "smart_charge", {"options": ["smart_charge", "max_charge", "paused"]}, translation_key=True),
            _e("button", "OH1_approve", "approve", translation_key=True),
            _e("sensor", "OH1_status", "status", "plugged_in", translation_key=True),
            _e("switch", "OH1_price_cap", "price_cap", "on", translation_key=True),
            _e("switch", "OH1_solar_boost", "solar_boost", "off", translation_key=True),
        ),
        {
            "charge_control": "select.ohme_charge_mode",
            "path": {"kind": "select_approve", "start_option": "max_charge", "stop_option": "paused"},
            "current_limit": None,
            "current_control": "",
            "energy_register": None,
            "session_energy_register": None,
            "charging_state": "sensor.ohme_status",
            "current_entities": [],
            "conflicts": ["switch.ohme_price_cap"],
        },
    ),
    "chargepoint": Shape(
        "chargepoint",
        "13983833",
        (
            _e("button", "13983833_start_charging_session", "start_charging_session"),
            _e("button", "13983833_stop_charging_session", "stop_charging_session"),
            _e("button", "13983833_restart_charger", "restart_charger"),
            _e("select", "13983833_charging_amperage_limit", "charging_amperage_limit", "32", {"options": ["8", "16", "32"]}),
        ),
        {
            "charge_control": "button.chargepoint_start_charging_session",
            "path": {
                "kind": "buttons",
                "start_entity_id": "button.chargepoint_start_charging_session",
                "stop_entity_id": "button.chargepoint_stop_charging_session",
            },
            "current_limit": None,
            "current_control": "",
            "energy_register": None,
            "session_energy_register": None,
            "charging_state": None,
            "current_entities": [],
        },
    ),
}


def register_shape(hass: HomeAssistant, shape: Shape, *, suffix: str = "") -> dict[str, str]:
    """Register a shape's device(s) and entities, and give every enabled entity its state.

    Returns `{"device_id", "installation_device_id"?}`. `suffix` keeps a second charger of the same
    platform apart (serials, unique ids and entity ids all change with it).
    """
    config_entry = MockConfigEntry(domain=shape.platform, entry_id=f"{shape.platform}{suffix}")
    config_entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    out: dict[str, str] = {}
    via_device_id: str | None = None
    if shape.installation:
        installation = devices.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            identifiers={(shape.platform, f"installation{suffix}")},
            name="Installation",
        )
        out["installation_device_id"] = installation.id
        via_device_id = installation.id
        _register(hass, registry, config_entry, shape, installation.id, shape.installation, suffix)
    device = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(shape.platform, f"{shape.serial}{suffix}")},
        name=f"{shape.platform} charger{suffix}",
        via_device_id=via_device_id,
    )
    out["device_id"] = device.id
    _register(hass, registry, config_entry, shape, device.id, shape.entities, suffix)
    return out


def _register(
    hass: HomeAssistant,
    registry: er.EntityRegistry,
    config_entry: MockConfigEntry,
    shape: Shape,
    device_id: str,
    entities: tuple[E, ...],
    suffix: str,
) -> None:
    for entity in entities:
        entry = registry.async_get_or_create(
            entity.domain,
            shape.platform,
            f"{entity.unique_id}{suffix}",
            suggested_object_id=f"{shape.platform}_{entity.key}{suffix}",
            config_entry=config_entry,
            device_id=device_id,
            translation_key=entity.key if entity.translation_key else None,
            original_device_class=entity.attrs.get("device_class"),
            capabilities=(
                {"state_class": entity.attrs["state_class"]} if "state_class" in entity.attrs else None
            ),
            unit_of_measurement=entity.attrs.get("unit_of_measurement"),
            disabled_by=RegistryEntryDisabler.INTEGRATION if entity.disabled else None,
        )
        if not entity.disabled:
            hass.states.async_set(entry.entity_id, entity.state, entity.attrs)
