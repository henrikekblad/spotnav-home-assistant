"""The `progress` block and `live.measured_current_a` from a real charger: what each kind of charger
measures (its own current sensors, a smart plug's power, the site's measurement of it), and the open
session's start level and energy.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.dashboard import serialize_dashboard
from custom_components.spotnav.const import (
    CONF_CHARGER_CURRENT_ENTITIES,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_POWER_ENTITY,
)
from custom_components.spotnav.runtime import charger_data
from custom_components.spotnav.sessions.model import ChargeSession
from custom_components.spotnav.site.measurement_source import PhaseMeasurementSource, source_to_dict
from custom_components.spotnav.site.site_capacity import PHASES

from .helpers import make_entry, make_site_entry, set_charger_phases

pytestmark = pytest.mark.usefixtures("offline_relay")


def dashboard(hass: HomeAssistant, entry) -> dict:
    return serialize_dashboard(dashboard_api.capture_dashboard(hass, entry), can_act=True)


async def charger(hass: HomeAssistant, **extra) -> object:
    hass.states.async_set("switch.charger_a", "on")
    entry = make_entry(
        hass,
        entry_id="entry_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
        extra=extra or None,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    set_charger_phases(hass, "entry_a", 1)
    return entry


async def test_a_charger_with_current_sensors_reports_its_measured_current(hass: HomeAssistant) -> None:
    sensors = [f"sensor.charger_a_current_l{n}" for n in (1, 2, 3)]
    for entity_id, value in zip(sensors, ("15.8", "0.2", "0.1")):
        hass.states.async_set(entity_id, value, {"unit_of_measurement": "A", "device_class": "current"})
    entry = await charger(hass, **{CONF_CHARGER_CURRENT_ENTITIES: sensors})
    answer = dashboard(hass, entry)
    assert answer["live"]["charging"] is True
    assert answer["live"]["measured_current_a"] == pytest.approx(15.8)
    progress = answer["progress"]
    # A charge with no plan and no level: the open bar, at the measured 15.8 A on one phase.
    assert progress["basis"] == "open"
    assert progress["moving"] is True
    assert progress["power_source"] == "measured"
    assert progress["power_kw"] == pytest.approx(3.63, abs=0.01)


async def test_a_measured_zero_stands_the_bar_still(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.charger_a_current", "0", {"unit_of_measurement": "A"})
    entry = await charger(hass, **{CONF_CHARGER_CURRENT_ENTITIES: ["sensor.charger_a_current"]})
    progress = dashboard(hass, entry)["progress"]
    assert progress["moving"] is False and progress["power_kw"] is None


async def test_a_smart_plug_reports_its_measured_power(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "sensor.plug_power", "2210", {"unit_of_measurement": "W", "device_class": "power"}
    )
    entry = await charger(hass, **{CONF_POWER_ENTITY: "sensor.plug_power"})
    answer = dashboard(hass, entry)
    assert answer["live"]["measured_current_a"] is None
    assert answer["progress"]["power_kw"] == pytest.approx(2.21)
    assert answer["progress"]["power_source"] == "measured"


async def test_a_charger_without_a_measurement_states_the_planned_power(hass: HomeAssistant) -> None:
    entry = await charger(hass)
    answer = dashboard(hass, entry)
    assert answer["live"]["measured_current_a"] is None
    progress = answer["progress"]
    assert progress["power_source"] != "measured"


async def test_the_sites_measurement_of_the_charger_stands_in(hass: HomeAssistant) -> None:
    for phase, value in zip(PHASES, ("9.5", "9.9", "9.7")):
        hass.states.async_set(f"sensor.wallbox_{phase.lower()}", value, {"unit_of_measurement": "A"})
    entry = await charger(hass)
    site = make_site_entry(
        hass,
        entry_id="site_a",
        charger_entry_ids=["entry_a"],
        phase_wiring={
            "entry_a": {
                "phases": 3,
                "phase": None,
                "min_current_a": 6.0,
                CONF_MEASURED_CURRENT_SOURCE: source_to_dict(
                    PhaseMeasurementSource(
                        kind="separate_entities",
                        entity_ids={p: f"sensor.wallbox_{p.lower()}" for p in PHASES},
                    )
                ),
            }
        },
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    answer = dashboard(hass, entry)
    assert answer["live"]["measured_current_a"] == pytest.approx(9.9)
    assert answer["progress"]["power_source"] == "measured"


async def test_no_progress_while_the_charger_is_off(hass: HomeAssistant) -> None:
    entry = await charger(hass)
    hass.states.async_set("switch.charger_a", "off")
    await hass.async_block_till_done()
    answer = dashboard(hass, entry)
    assert answer["live"]["charging"] is False
    assert answer["progress"] is None


async def test_the_open_session_gives_the_start_and_measured_energy(hass: HomeAssistant) -> None:
    entry = await charger(hass)
    data = charger_data(hass, "entry_a")
    assert data is not None and data.sessions is not None
    start = datetime.fromisoformat("2026-09-22T05:20:00+00:00")
    session = ChargeSession(
        id="entry_a-1", charger_id="entry_a", start=start, end=None, energy_kwh=4.256,
        energy_source="register", priced_kwh=0.0, cost_minor=None, reference_cost_minor=None,
        currency=None, major_unit=None, minor_unit=None, started_by="manual", strategy=None,
        vehicle_id=None, vehicle_name=None, solar_kwh=0.0, solar_known_kwh=0.0,
        last_register_kwh=10.0, last_sample_at=start, start_soc_percent=41.04,
    )
    data.sessions._session = session  # noqa: SLF001 - the recorder's open session, set for the read
    progress = dashboard(hass, entry)["progress"]
    assert datetime.fromisoformat(progress["started_at"]) == start
    assert progress["start_soc_percent"] == 41.0
    assert progress["delivered_kwh"] == 4.26
    session.energy_source = "estimated"
    assert dashboard(hass, entry)["progress"]["delivered_kwh"] is None
