"""Field report: an EV6 (kia_uvo) and a mock "Testbil" (MQTT) at one OCPP charger; identification decided the
Testbil by `plug_sensor` although its plug sensor never went on."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service, MockConfigEntry

from custom_components.spotnav.planning.auto_settings import PauseIntent, TargetSocIntent
from custom_components.spotnav.runtime import charger_data, domain_data
from custom_components.spotnav.vehicles.identification_sources import identification_sources

from .world import setup_charger

pytestmark = pytest.mark.usefixtures("offline_relay")

HA_START = datetime(2026, 10, 7, 15, 40, 56, tzinfo=timezone.utc)
TESTBIL_MADE = datetime(2026, 10, 7, 16, 9, 31, tzinfo=timezone.utc)
UNPLUG = datetime(2026, 10, 7, 17, 12, 48, tzinfo=timezone.utc)
PLUG_IN = datetime(2026, 10, 7, 17, 12, 49, tzinfo=timezone.utc)


def _entity(hass: HomeAssistant, platform: str, device_id: str, domain: str, key: str, object_id: str, state: str,
            **attributes: Any) -> str:
    entry = er.async_get(hass).async_get_or_create(
        domain, platform, f"{device_id}_{key}", device_id=device_id, suggested_object_id=object_id,
        original_name=key.replace("_", " "),
    )
    hass.states.async_set(entry.entity_id, state, attributes)
    return entry.entity_id


class Field:
    def __init__(self, hass: HomeAssistant, freezer: Any) -> None:
        self.hass, self.freezer = hass, freezer
        self.connected: bool | None = True
        self.echoed: list[str] = []

    async def build(self) -> None:
        hass, freezer = self.hass, self.freezer
        freezer.move_to(HA_START)
        MockConfigEntry(domain="kia_uvo", entry_id="kia").add_to_hass(hass)
        ev6 = dr.async_get(hass).async_get_or_create(config_entry_id="kia", identifiers={("kia_uvo", "ev6")}, name="EV6")
        self.ev6 = ev6.id
        _entity(hass, "kia_uvo", ev6.id, "sensor", "ev_battery_level", "ev6_ev_battery_level", "62",
                device_class="battery", unit_of_measurement="%")
        _entity(hass, "kia_uvo", ev6.id, "sensor", "ev_driving_range", "ev6_ev_range", "300",
                device_class="distance", unit_of_measurement="km")
        self.ev6_plug = _entity(hass, "kia_uvo", ev6.id, "binary_sensor", "ev_battery_is_plugged_in",
                                "ev6_ev_battery_plug", "off", device_class="plug")
        self.ev6_port = _entity(hass, "kia_uvo", ev6.id, "binary_sensor", "ev_charge_port_door_is_open",
                                "ev6_ev_charge_port", "on", device_class="door")
        _entity(hass, "kia_uvo", ev6.id, "switch", "ev_charge_port", "ev6_charge_port", "on")
        _entity(hass, "kia_uvo", ev6.id, "device_tracker", "location", "ev6_location", "home", source_type="gps")
        self.entry = await setup_charger(hass, title="HALO")
        controller = charger_data(hass, self.entry.entry_id).controller
        controller.adapter.vehicle_connected = lambda: self.connected  # type: ignore[method-assign]
        async_mock_service(hass, "switch", "turn_on")
        async_mock_service(hass, "switch", "turn_off")

        async def refresh(call: ServiceCall) -> None:
            # Home Assistant re-writes what an MQTT entity holds, and the Kia's coordinator re-writes its cloud
            # cache: the plug's stale "off" from hours ago gets a new `last_reported`, the car said nothing new.
            for entity_id in call.data["entity_id"]:
                state = hass.states.get(entity_id)
                if state is not None:
                    hass.states.async_set(entity_id, state.state, state.attributes, force_update=True)
                    self.echoed.append(entity_id)

        hass.services.async_register("homeassistant", "update_entity", refresh)
        # The refresh limiter's minute on the test's clock, as it passes in the field.
        from homeassistant.util import dt as dt_util

        from custom_components.spotnav.vehicles.vehicle_refresh import limiter_for

        limiter_for(hass).clock = lambda: dt_util.utcnow().timestamp()
        freezer.move_to(TESTBIL_MADE)
        MockConfigEntry(domain="mqtt", entry_id="mqtt").add_to_hass(hass)
        bil = dr.async_get(hass).async_get_or_create(config_entry_id="mqtt", identifiers={("mqtt", "testbil")},
                                                     name="Testbil")
        self.testbil = bil.id
        _entity(hass, "mqtt", bil.id, "sensor", "battery_level", "testbil_battery_level", "55",
                device_class="battery", unit_of_measurement="%")
        _entity(hass, "mqtt", bil.id, "sensor", "range", "testbil_range", "250", device_class="distance",
                unit_of_measurement="km")
        _entity(hass, "mqtt", bil.id, "sensor", "battery_capacity", "testbil_battery_capacity", "64",
                device_class="energy_storage", unit_of_measurement="kWh")
        self.testbil_plug = _entity(hass, "mqtt", bil.id, "binary_sensor", "charging_cable",
                                    "testbil_charging_cable", "off", device_class="plug")
        _entity(hass, "mqtt", bil.id, "device_tracker", "location", "testbil_location", "home", source_type="gps")
        store = domain_data(hass).auto_store
        await store.async_update(
            self.entry.entry_id,
            mutate=lambda s: replace(s, target=TargetSocIntent(vehicle_id=bil.id, target_percent=80.0)),
        )
        await self.observe()

    async def observe(self) -> None:
        state = self.hass.states.get("switch.charger_a")
        self.hass.states.async_set("switch.charger_a", "off", {"t": state.attributes.get("t", 0) + 1 if state else 0})
        await self.hass.async_block_till_done()

    async def at(self, moment: datetime) -> None:
        self.freezer.move_to(moment)
        async_fire_time_changed(self.hass, moment)
        await self.hass.async_block_till_done()

    @property
    def identifier(self) -> Any:
        return charger_data(self.hass, self.entry.entry_id).identifier

    @property
    def target(self) -> str | None:
        return domain_data(self.hass).auto_store.settings(self.entry.entry_id).target.vehicle_id


async def test_the_kia_charge_port_door_is_no_plug_and_each_car_reads_its_own_sensor(
    hass: HomeAssistant, freezer: Any
) -> None:
    field = Field(hass, freezer)
    await field.build()
    assert identification_sources(hass, field.ev6)[0].entity_id == field.ev6_plug
    assert identification_sources(hass, field.ev6)[0].candidates == (field.ev6_plug,), "the port door is no plug"
    assert identification_sources(hass, field.testbil)[0].entity_id == field.testbil_plug


async def test_our_own_re_read_is_no_report_that_a_car_is_not_plugged_in(hass: HomeAssistant, freezer: Any) -> None:
    field = Field(hass, freezer)
    await field.build()
    await field.at(UNPLUG)
    field.connected = False
    await field.observe()
    await field.at(PLUG_IN)
    field.connected = True
    await field.observe()
    await field.at(PLUG_IN + timedelta(minutes=3, seconds=1))
    await field.at(PLUG_IN + timedelta(minutes=10))
    assert field.ev6_plug in field.echoed, "the re-read reached the Kia's cached plug"
    block = field.identifier.dashboard()
    assert field.identifier.method != "plug_sensor", f"decided {block} from a re-written cache"
    assert block["state"] == "asking"
    assert field.target == field.testbil, "nothing decided, the chosen car stays"


async def test_a_field_report_shows_which_entity_and_state_each_car_was_judged_by(
    hass: HomeAssistant, freezer: Any
) -> None:
    field = Field(hass, freezer)
    await field.build()
    await field.at(UNPLUG)
    field.connected = False
    await field.observe()
    await field.at(PLUG_IN)
    field.connected = True
    await field.observe()
    hass.states.async_set(field.testbil_plug, "on", {"device_class": "plug"})
    await hass.async_block_till_done()
    block = field.identifier.dashboard()
    assert block["method"] == "plug_sensor" and field.target == field.testbil
    evidence = {item["vehicle_id"]: item for item in block["evidence"]}
    assert evidence[field.testbil]["verdict"] == "plugged_in"
    assert evidence[field.testbil]["plug"]["entity_id"] == field.testbil_plug
    assert evidence[field.testbil]["plug"]["state"] == "on"
    assert evidence[field.ev6]["plug"] == {
        "entity_id": field.ev6_plug, "state": "off",
        "changed": HA_START.isoformat(), "reported": HA_START.isoformat(),
    }
    assert evidence[field.ev6]["location"] == {
        "entity_id": "device_tracker.ev6_location", "home": True, "reported": HA_START.isoformat(),
    }
    diagnostics = field.identifier.diagnostics()
    assert diagnostics["evidence"] == block["evidence"]
