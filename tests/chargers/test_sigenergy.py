"""Sigenergy's DC charger (EVDC) as a SpotNav charger (`execution/chargers/sigenergy.py`).

Built from the TypQxQ "Sigenergy ESS" 2.0.3 entity shapes (`charger_shapes.SHAPES["sigen"]`): detection of the DC
charger among the plant's other devices, the plug state read from its running state, the car's level only during a
session, Start, Stop and power writes in kW, a load-balancing cut written as kW, the car as its own load for the
battery plan and the house, and the careful timing until the tester has answered.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service, MockConfigEntry

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CHARGER_PLATFORM,
    CONF_CHARGING_STATE,
    CONF_ENTRY_TYPE,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_PHASE_WIRING,
    CONF_VOLTAGE_BETWEEN_PHASES_V,
    CURRENT_CONTROL_POWER,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    MEASUREMENT_MODE_DERIVED,
)
from custom_components.spotnav.execution.charger_connection import CHARGING, CONNECTED, DISCONNECTED, ERROR, UNKNOWN
from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_BELOW_MINIMUM,
    ASSIGN_RATE_LIMITED,
    ASSIGN_UNCHANGED,
    WRITE_REGULATOR,
    WRITE_SESSION_START,
)
from custom_components.spotnav.execution.chargers.sigenergy import (
    EVDC_MIN_POWER_KW,
    EVDC_SETTLE_S,
    kw_per_amp,
)
from custom_components.spotnav.execution.controller import power_range
from custom_components.spotnav.execution.target_stop import charger_soc_live, choose_soc_reading, SocReading
from custom_components.spotnav.flows.charger_detection import detect_charger, is_charger_device
from custom_components.spotnav.execution.charger_profiles import profile_for
from custom_components.spotnav.site.detected_hass import _has_charger
from custom_components.spotnav.site.measurement_source import (
    PhaseMeasurementSource,
    read_phase_measurement,
    source_from_dict,
    source_to_dict,
)
from custom_components.spotnav.site.regulator import _decide_phase
from custom_components.spotnav.site.site_capacity import classify_current, PhaseValue

from ..charger_helpers import adapter_for, Clock, detected_config
from ..charger_shapes import register_shape, SHAPES

LIMIT = "number.sigen_dc_charger_max_charging_power_limit"
START = "button.sigen_dc_charger_start"
STOP = "button.sigen_dc_charger_stop"
OUTPUT = "sensor.sigen_dc_charger_output_power"
RATED = "sensor.sigen_dc_charger_rated_charging_power"
STATE = "sensor.sigen_dc_charger_running_state"
SOC = "sensor.sigen_dc_charger_vehicle_soc"
POWER_LIMIT_ATTRS = {"device_class": "power", "unit_of_measurement": "kW", "min": 0, "max": 100, "step": 0.001}
KW_PER_A_400 = math.sqrt(3) * 400 / 1000


def _found(hass: HomeAssistant):
    ids = register_shape(hass, SHAPES["sigen"])
    found = detect_charger(hass, ids["device_id"])
    assert found is not None
    return ids, found


def _set_limit(hass: HomeAssistant, kw: float) -> None:
    hass.states.async_set(LIMIT, str(kw), POWER_LIMIT_ATTRS)


def _mock_writes(hass: HomeAssistant) -> list[Any]:
    """`number.set_value` that moves the number's state as the integration would."""
    calls: list[Any] = []

    async def set_value(call) -> None:
        calls.append(call)
        _set_limit(hass, call.data["value"])

    hass.services.async_register("number", "set_value", set_value)
    return calls


# -- detection --------------------------------------------------------------------------------------


async def test_the_plants_other_devices_are_not_chargers(hass: HomeAssistant) -> None:
    """The plant, the inverters and the battery share the `sigen` platform: only the DC charger's device is one."""
    ids, found = _found(hass)
    assert found.role == "charger" and found.current_control == CURRENT_CONTROL_POWER
    entry = hass.config_entries.async_get_entry("sigen")
    inverter = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("sigen", "inverter_1")}, name="Sigen Inverter"
    )
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "number",
        "sigen",
        "sig1_inverter_1_ess_max_charging_limit",
        config_entry=entry,
        device_id=inverter.id,
        translation_key="inverter_ess_max_charging_limit",
    )
    registry.async_get_or_create(
        "sensor",
        "sigen",
        "sig1_inverter_1_ess_battery_soc",
        config_entry=entry,
        device_id=inverter.id,
        translation_key="inverter_ess_battery_soc",
    )

    other = detect_charger(hass, inverter.id)

    assert other is not None and other.role is None and "not_a_charger_device" in other.notes
    profile = profile_for("sigen")
    assert profile is not None
    assert is_charger_device(profile, er.async_entries_for_device(registry, ids["device_id"]))
    assert not is_charger_device(profile, er.async_entries_for_device(registry, inverter.id))


async def test_a_plant_without_a_dc_charger_is_no_charger_left_out(hass: HomeAssistant) -> None:
    plant = MockConfigEntry(domain="sigen", entry_id="plant_only")
    plant.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor", "sigen", "plant_only_plant_battery_soc", config_entry=plant, translation_key="plant_ess_soc"
    )
    assert _has_charger(er.async_get(hass), plant) is False

    register_shape(hass, SHAPES["sigen"])
    assert _has_charger(er.async_get(hass), hass.config_entries.async_get_entry("sigen")) is True


# -- the plug state from the running state ----------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "connected", "connection"),
    [
        ("idle", False, DISCONNECTED),
        ("occupied", True, CONNECTED),
        ("preparing_comm", True, CONNECTED),
        ("preparing_insulation", True, CONNECTED),
        ("scheduled", True, CONNECTED),
        ("charging", True, CHARGING),
        ("discharging", True, CONNECTED),
        # Read after a Stop of ours with the car still plugged in: neither a plug-in nor an unplug.
        ("ended", None, UNKNOWN),
        ("fault", None, ERROR),
        ("alarm", None, ERROR),
        ("charger_unavailable", None, UNKNOWN),
    ],
)
async def test_the_plug_state_comes_from_the_running_state(
    hass: HomeAssistant, state: str, connected: bool | None, connection: str
) -> None:
    _ids, found = _found(hass)
    adapter = adapter_for(hass, found)
    hass.states.async_set(STATE, state, {"device_class": "enum"})

    assert adapter.charger_vehicle_connected() is connected
    assert adapter.connection() == (connection, STATE)
    assert adapter.charging_state() is (state == "charging")
    assert adapter.reports_connection


# -- the car's level --------------------------------------------------------------------------------


def _reading(source: str, soc: float | None, age_s: float = 10.0) -> SocReading:
    return SocReading(soc_percent=soc, source=source, entity_id=f"sensor.{source}_soc", vehicle_id=None, age_s=age_s)


async def test_the_chargers_level_is_unknown_outside_a_session(hass: HomeAssistant) -> None:
    """Before a session the DC charger reads 0 %: that is unknown, never the car's level."""
    _ids, found = _found(hass)
    data = detected_config(found)
    hass.states.async_set(SOC, "0", {"device_class": "battery", "unit_of_measurement": "%"})

    for state in ("idle", "occupied", "preparing_comm", "ended", "unavailable"):
        hass.states.async_set(STATE, state)
        live = charger_soc_live(hass, data)
        assert live is False
        chosen = choose_soc_reading(_reading("charger", 0.0), None, charger_live=live)
        assert chosen is not None and chosen.soc_percent is None and chosen.source == "charger"

    hass.states.async_set(STATE, "charging")
    assert charger_soc_live(hass, data) is True
    assert choose_soc_reading(_reading("charger", 55.0), None, charger_live=True).soc_percent == 55.0
    # A 0 % read during the session's first moments is not a level either.
    assert choose_soc_reading(_reading("charger", 0.0), None, charger_live=True).soc_percent is None


def test_a_car_integration_is_preferred() -> None:
    vehicle_fresh = _reading("vehicle", 48.0, age_s=60.0)
    vehicle_stale = _reading("vehicle", 40.0, age_s=3600.0)
    charger = _reading("charger", 52.0)

    # Outside a session the car's reading, fresh or not.
    assert choose_soc_reading(charger, vehicle_stale, charger_live=False) is vehicle_stale
    # During a session a fresh car reading still wins; a stale one gives way to the charger's live one.
    assert choose_soc_reading(charger, vehicle_fresh, charger_live=True) is vehicle_fresh
    assert choose_soc_reading(charger, vehicle_stale, charger_live=True) is charger
    # Another charger keeps its rule: its own reading first while it has one.
    assert choose_soc_reading(charger, vehicle_fresh, charger_live=None) is charger


async def test_another_chargers_reading_is_always_live(hass: HomeAssistant) -> None:
    assert charger_soc_live(hass, {CONF_CHARGER_PLATFORM: "peblar"}) is None
    assert charger_soc_live(hass, {}) is None


# -- Start, Stop and the power ----------------------------------------------------------------------


async def test_start_and_stop_press_the_buttons(hass: HomeAssistant) -> None:
    _ids, found = _found(hass)
    presses = async_mock_service(hass, "button", "press")
    adapter = adapter_for(hass, found)

    assert await adapter.async_start(16) is True
    assert await adapter.async_stop() is True

    assert [call.data["entity_id"] for call in presses] == [START, STOP]
    assert [record["kind"] for record in adapter.current.timing.records()] == ["start", "stop"]
    adapter.cancel_pending_reads()


@pytest.mark.parametrize(
    ("amps", "kw"),
    [
        (6, 4.156),  # SpotNav's floor: 6 A on three phases at 400 V
        (16, 11.085),
        (17, 12),  # the rating's own current writes the full rating, not 11.78 kW
        (25, 12),  # never above the rating
    ],
)
async def test_a_current_is_written_as_the_power_of_three_phases(hass: HomeAssistant, amps: int, kw: float) -> None:
    _ids, found = _found(hass)
    _set_limit(hass, 1.0)
    calls = _mock_writes(hass)
    adapter = adapter_for(hass, found)

    assert await adapter.async_set_current(amps, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED

    assert calls[-1].data == {"entity_id": LIMIT, "value": kw}
    assert adapter.current.read_back_a() == min(amps, 17)
    adapter.cancel_pending_reads()


async def test_below_the_floor_or_the_least_power_nothing_is_written(hass: HomeAssistant) -> None:
    _ids, found = _found(hass)
    calls = _mock_writes(hass)
    adapter = adapter_for(hass, found)
    # The floor is the least power as a current per phase, rounded up to the hundredth: 1.5 kW / 0.6928 = 2.17 A.
    assert adapter.current.min_a() == 2.17
    assert adapter.min_current_a == adapter.min_start_current_a == 2.17
    assert await adapter.async_set_current(2, reason=WRITE_SESSION_START) == ASSIGN_BELOW_MINIMUM
    assert await adapter.async_set_current(2.16, reason=WRITE_SESSION_START) == ASSIGN_BELOW_MINIMUM
    _set_limit(hass, 12)
    assert await adapter.async_set_current(17, reason=WRITE_SESSION_START) == ASSIGN_UNCHANGED
    assert calls == []


async def test_the_floor_is_written_at_the_least_power_never_below_it(hass: HomeAssistant) -> None:
    """The DC charger's floor (2.17 A at 400 V) writes 1.50 kW, not the 4.2 kW of an AC charger's 6 A, and no write
    comes out below `EVDC_MIN_POWER_KW`, also once rounded down to the number's step; it reads back as the floor."""
    _ids, found = _found(hass)
    calls = _mock_writes(hass)
    adapter = adapter_for(hass, found)
    _set_limit(hass, 12)

    assert await adapter.async_set_current(2.17, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED

    assert calls[-1].data["value"] == pytest.approx(1.503, abs=0.001)
    assert calls[-1].data["value"] >= EVDC_MIN_POWER_KW
    assert adapter.current.setpoint_a() == 2.17
    assert adapter.current.read_back_a() == 2.17
    # Above the floor the steps are whole amperes again: 2.9 kW reads back as 4 A.
    _set_limit(hass, round(4 * KW_PER_A_400, 3))
    assert adapter.current.read_back_a() == 4
    # A step so coarse that the floor would be written below the least power (1.5 kW to 1 kW): nothing is written.
    hass.states.async_set(LIMIT, "12", {**POWER_LIMIT_ATTRS, "step": 1})
    written = len(calls)
    assert await adapter.async_set_current(2.17, reason=WRITE_SESSION_START) == ASSIGN_BELOW_MINIMUM
    assert len(calls) == written
    assert all(call.data["value"] >= EVDC_MIN_POWER_KW for call in calls)


async def test_on_an_it_network_the_floor_follows_the_voltage(hass: HomeAssistant) -> None:
    """At 230 V between phases one ampere is 0.40 kW: the floor is 3.77 A, still 1.5 kW."""
    _ids, found = _found(hass)
    MockConfigEntry(
        domain=DOMAIN,
        entry_id="evdc",
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER, CONF_CHARGE_CONTROL: START, CONF_VOLTAGE_BETWEEN_PHASES_V: 230},
    ).add_to_hass(hass)
    adapter = adapter_for(hass, found)
    floor = adapter.current.min_a()
    assert floor == 3.77
    assert floor * kw_per_amp(230) >= EVDC_MIN_POWER_KW > (floor - 0.01) * kw_per_amp(230)


async def test_the_power_follows_the_sites_voltage(hass: HomeAssistant) -> None:
    """On an IT network one ampere per phase is 0.40 kW: the inverter's line current is what the fuse carries."""
    _ids, found = _found(hass)
    MockConfigEntry(
        domain=DOMAIN,
        entry_id="evdc",
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER, CONF_CHARGE_CONTROL: START, CONF_VOLTAGE_BETWEEN_PHASES_V: 230},
    ).add_to_hass(hass)
    calls = _mock_writes(hass)
    adapter = adapter_for(hass, found)

    assert await adapter.async_set_current(16, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED

    assert calls[-1].data["value"] == pytest.approx(16 * math.sqrt(3) * 230 / 1000, abs=0.001)
    # The range starts at the first whole ampere at or above the floor of 3.77 A.
    assert power_range(adapter.current) == {
        "min_a": 4,
        "max_a": 30,
        "source": "current_limit",
        "kw_per_a": round(kw_per_amp(230), 4),
    }
    adapter.cancel_pending_reads()


async def test_the_card_is_given_the_range_in_amperes_and_kw_per_ampere(hass: HomeAssistant) -> None:
    """The range starts at the first whole ampere at or above the floor (2.17 A at 400 V): 3 A, 2.08 kW, not 6 A."""
    _ids, found = _found(hass)
    adapter = adapter_for(hass, found)

    assert power_range(adapter.current) == {"min_a": 3, "max_a": 17, "source": "current_limit", "kw_per_a": 0.6928}
    assert power_range(adapter.current)["min_a"] * KW_PER_A_400 >= EVDC_MIN_POWER_KW
    # Without its rating sensor the rating last read stands, never the number's own maximum (100 kW).
    hass.states.async_remove(RATED)
    assert power_range(adapter.current)["max_a"] == 17


# -- careful timing ---------------------------------------------------------------------------------


async def test_a_raise_waits_for_the_settle_and_a_lowering_for_ten_seconds(hass: HomeAssistant) -> None:
    _ids, found = _found(hass)
    _set_limit(hass, 1.0)
    calls = _mock_writes(hass)
    clock = Clock()
    adapter = adapter_for(hass, found, clock=clock)

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    clock.advance(5)
    assert await adapter.async_set_current(8, reason=WRITE_REGULATOR) == ASSIGN_RATE_LIMITED
    clock.advance(6)
    assert await adapter.async_set_current(8, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    clock.advance(EVDC_SETTLE_S - 1)
    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_RATE_LIMITED
    clock.advance(2)
    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED

    assert [call.data["value"] for call in calls] == [6.928, 5.542, 8.313]
    adapter.cancel_pending_reads()


async def test_the_diagnostics_keep_what_the_output_did_after_each_write(hass: HomeAssistant) -> None:
    """The tester's debug report answers how fast the charger follows a new limit."""
    _ids, found = _found(hass)
    _set_limit(hass, 1.0)
    _mock_writes(hass)
    adapter = adapter_for(hass, found)
    hass.states.async_set(OUTPUT, "2.0", {"unit_of_measurement": "kW", "device_class": "power"})

    assert await adapter.async_set_current(16, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    start = adapter.current.timing.records()[0]
    assert start["kind"] == "limit" and start["limit_kw"] == 11.085 and start["output_kw_before"] == 2.0

    now = dt_util.utcnow()
    async_fire_time_changed(hass, now + timedelta(seconds=6))
    await hass.async_block_till_done()
    hass.states.async_set(OUTPUT, "10.9", {"unit_of_measurement": "kW", "device_class": "power"})
    async_fire_time_changed(hass, now + timedelta(seconds=16))
    await hass.async_block_till_done()

    record = adapter.diagnostics(START)["current_state"]["timing"][0]
    assert [sample["output_kw"] for sample in record["samples"]] == [2.0, 10.9]
    assert record["followed_after_s"] == 15.0
    adapter.cancel_pending_reads()


# -- load balancing ---------------------------------------------------------------------------------


async def test_the_output_power_reads_as_the_current_of_three_phases(hass: HomeAssistant) -> None:
    hass.states.async_set(OUTPUT, "11.085", {"unit_of_measurement": "kW"})
    source = PhaseMeasurementSource(kind="power", entity_id=OUTPUT, voltage_between_phases_v=400.0)
    assert source_from_dict(source_to_dict(source)) == source

    values = read_phase_measurement(hass, source, classify_current)

    assert {phase: round(value.value, 2) for phase, value in values.items()} == {"L1": 16.0, "L2": 16.0, "L3": 16.0}
    # The car giving energy back (a negative output) draws nothing.
    hass.states.async_set(OUTPUT, "-3", {"unit_of_measurement": "kW"})
    assert read_phase_measurement(hass, source, classify_current)["L1"].value == 0.0
    assert source_from_dict({**source_to_dict(source), "voltage_between_phases_v": 12}) is None


async def test_an_overload_lowers_the_power_limit_like_a_current(hass: HomeAssistant) -> None:
    """3 A over the fuse on a phase while the charger delivers 11.1 kW (16 A a phase): it is given 13 A, written as
    9.0 kW."""
    _ids, found = _found(hass)
    _set_limit(hass, 11.085)
    calls = _mock_writes(hass)
    adapter = adapter_for(hass, found)
    hass.states.async_set(OUTPUT, "11.1", {"unit_of_measurement": "kW"})
    measured = read_phase_measurement(
        hass, PhaseMeasurementSource(kind="power", entity_id=OUTPUT, voltage_between_phases_v=400.0), classify_current
    )["L1"]

    proposed, reason = _decide_phase(
        safe_uncredited_headroom_a=-3.0,
        signed_active_power_w=5000.0,
        signed_active_power_age_s=1.0,
        signed_active_power_reason=None,
        voltage_v=230.0,
        requested_current_a=16.0,
        measured_current=PhaseValue(measured.value, 1.0, None),
        confirmed_dir="net_import",
        max_age_s=120.0,
        zero_margin_w=50.0,
        help_margin_factor=0.5,
        min_current_a=6.0,
    )
    assert reason == "reducing_current_due_to_active_import_overload" and proposed == 13.0

    assert await adapter.async_set_current(int(proposed), reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    assert calls[-1].data["value"] == pytest.approx(13 * KW_PER_A_400, abs=0.001)
    adapter.cancel_pending_reads()


# -- the car as its own load ------------------------------------------------------------------------


async def test_the_car_is_its_own_load_for_the_battery_plan_and_the_house(hass: HomeAssistant) -> None:
    """The site reads the DC charger's output as the car's draw (`site_car_w`, what the battery plan and the hour's
    house figure subtract), and a site without a source for it gets one from its profile."""
    from custom_components.spotnav.execution.solar_execution import site_car_w
    from custom_components.spotnav.site.site_join import backfill_profile_measured_sources

    from ..helpers import make_entry, make_site_entry
    from ..world import controller_of, set_derived_site_entities

    _ids, found = _found(hass)
    async_mock_service(hass, "button", "press")
    _mock_writes(hass)
    hass.states.async_set(OUTPUT, "6.9", {"unit_of_measurement": "kW", "device_class": "power"})
    data = detected_config(found)
    charger = make_entry(
        hass,
        entry_id="evdc",
        charge_control=START,
        current_limit=LIMIT,
        webhook_id="webhook-evdc",
        title="Sigen DC Charger",
        current_control=CURRENT_CONTROL_POWER,
        extra={key: value for key, value in data.items() if key not in (CONF_CHARGE_CONTROL,)},
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    site_entry = make_site_entry(
        hass,
        entry_id="home",
        main_fuse_a=20.0,
        charger_entry_ids=[charger.entry_id],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=set_derived_site_entities(hass, "home", 2000.0),
        phase_wiring={charger.entry_id: {"phases": 3, "phase": None}},
    )

    filled = backfill_profile_measured_sources(hass, site_entry)
    assert filled is not None
    source = filled[CONF_PHASE_WIRING][charger.entry_id][CONF_MEASURED_CURRENT_SOURCE]
    assert source == {
        **source_to_dict(PhaseMeasurementSource(kind="power", entity_id=OUTPUT, voltage_between_phases_v=400.0)),
    }
    hass.config_entries.async_update_entry(site_entry, data=filled)
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site = controller_of(hass, site_entry.entry_id)
    hass.states.async_set(STATE, "charging", {"device_class": "enum"})

    car_w = site_car_w(hass, site)

    # 6.9 kW over three phases at 400 V, counted back at the site's 230 V per phase: within half a percent.
    assert car_w == pytest.approx(6900.0, rel=0.005)
    assert controller_of(hass, charger.entry_id).adapter.measured_current_a() == pytest.approx(6.9 / KW_PER_A_400)
    assert charger.data[CONF_CHARGING_STATE]["entity_id"] == STATE
    await hass.config_entries.async_unload(site_entry.entry_id)
    await hass.config_entries.async_unload(charger.entry_id)
    await hass.async_block_till_done()


# -- phases -----------------------------------------------------------------------------------------


async def test_the_cars_onboard_phases_do_not_limit_a_dc_charger(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.planning import phases as phases_module

    MockConfigEntry(domain=DOMAIN, entry_id="evdc", data={CONF_CHARGER_PLATFORM: "sigen"}).add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, entry_id="wallbox", data={CONF_CHARGER_PLATFORM: "peblar"}).add_to_hass(hass)
    monkeypatch.setattr(phases_module.vehicle_properties, "resolved_vehicle_id", lambda hass, entry_id: "car")
    monkeypatch.setattr(phases_module.vehicle_properties, "onboard_phases", lambda hass, vehicle_id: 1)

    assert phases_module.charging_phases(hass, "evdc").phases == 3
    assert phases_module.charging_phases(hass, "wallbox").phases == 1


# -- the app ----------------------------------------------------------------------------------------


def test_an_older_app_is_not_sent_the_kw_per_ampere() -> None:
    """An app's decoder refuses a range with an unknown key: `kw_per_a` goes only to one that reads `power_charger`."""
    from custom_components.spotnav.api.webhook import _for_app

    body = {"current_range": {"min_a": 6, "max_a": 17, "source": "current_limit", "kw_per_a": 0.6928}}

    assert _for_app(body, {"action": "dashboard"})["current_range"] == {
        "min_a": 6,
        "max_a": 17,
        "source": "current_limit",
    }
    assert _for_app(body, {"action": "dashboard", "reads": ["power_charger"]}) == body


async def test_a_number_whose_max_is_below_the_least_power_is_never_written(hass: HomeAssistant) -> None:
    """A power number that goes no higher than 1.2 kW: held to its `max`, any write would be below 1.5 kW, so none is."""
    _ids, found = _found(hass)
    calls = _mock_writes(hass)
    adapter = adapter_for(hass, found)
    hass.states.async_set(RATED, "12", {"unit_of_measurement": "kW"})
    hass.states.async_set(LIMIT, "1.0", {**POWER_LIMIT_ATTRS, "max": 1.2})
    # The rating says 12 kW, so the request itself is above 1.5 kW: only the number's `max` holds it below.
    assert adapter.current.kw_for(10) == pytest.approx(10 * KW_PER_A_400)

    assert await adapter.async_set_current(2.17, reason=WRITE_SESSION_START) == ASSIGN_BELOW_MINIMUM
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_BELOW_MINIMUM
    assert calls == []
