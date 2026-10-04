"""Shared test worlds: the price/Auto harnesses and the recording doubles built on the relay wire."""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service, MockConfigEntry

from custom_components.spotnav.vehicles import vehicle_properties
from custom_components.spotnav.planning.auto_settings import (
    DRIVER_TARGET_SOC,
    STRATEGY_SOLAR,
    TargetSocIntent,
)
from custom_components.spotnav.const import (
    CONF_MEASURED_CURRENT_SOURCE,
    DOMAIN,
    MEASUREMENT_MODE_DERIVED,
)
from custom_components.spotnav.execution.controller import ChargingController, ChargingPlan
from custom_components.spotnav.site.measurement_source import PhaseMeasurementSource, source_to_dict
from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController
from custom_components.spotnav.execution.solar_execution import (
    SolarExecutionCoordinator,
)

from .helpers import (
    add_percent_battery_sensor,
    future_window,
    make_entry,
    make_site_entry,
    set_charger_phases,
    set_current_sensor,
)
from .messages import register
from .relay import SE4
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.runtime import preview_for


ENTRY = "entry-a"

AMPERE = {"unit_of_measurement": "A", "min": 6, "max": 16}
PHASES = ("L1", "L2", "L3")


# --------------------------------------------------------------- config-entry worlds


def controller_of(hass: HomeAssistant, entry_id: str) -> Any:
    """The live controller of a charger or a site entry."""
    return hass.config_entries.async_get_entry(entry_id).runtime_data.controller


async def setup_charger(
    hass: HomeAssistant,
    *,
    entry_id: str = "entry_a",
    webhook_id: str = "webhook-a",
    charge_control: str = "switch.charger_a",
    current_limit: str | None = None,
    title: str | None = None,
) -> MockConfigEntry:
    """One real charger config entry, set up through Home Assistant itself."""
    hass.states.async_set(charge_control, "off")
    entry = make_entry(
        hass,
        entry_id=entry_id,
        charge_control=charge_control,
        current_limit=current_limit,
        webhook_id=webhook_id,
        title=title or entry_id,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def setup_site(hass: HomeAssistant, *, entry_id: str = "site_a", **site_kwargs: Any) -> MockConfigEntry:
    """One real site config entry (no member chargers unless `charger_entry_ids` says so)."""
    site_kwargs.setdefault("charger_entry_ids", [])
    entry = make_site_entry(hass, entry_id=entry_id, **site_kwargs)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def go_auto(hass: HomeAssistant, entry_id: str = "entry_a", **changes: Any) -> Any:
    """Put a charger's preview into Auto through the reviewed settings path; returns its snapshot."""
    preview = preview_for(hass, entry_id)
    assert preview is not None
    changes.setdefault("area_id", SE4)
    changes.setdefault("amps", 10)
    set_charger_phases(hass, entry_id, changes.pop("phases", 1))
    changes.setdefault("departure_enabled", False)
    await preview.async_apply_settings(mutate=lambda settings: replace(settings, **changes))
    await hass.async_block_till_done()
    return preview.snapshot()


def entity_id(hass: HomeAssistant, entry_id: str, key: str, platform: str = "sensor") -> str:
    """The entity id a charger's entity was registered under, or a refusal naming it."""
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry_id}_{key}")
    assert found is not None, f"no {platform} entity {entry_id}_{key}"
    return found


async def call(hass: HomeAssistant, domain: str, service: str, data: dict[str, Any]) -> None:
    """One blocking service call, exactly as an automation would make it."""
    await hass.services.async_call(domain, service, data, blocking=True)
    await hass.async_block_till_done()


def settings_of(hass: HomeAssistant, entry_id: str) -> Any:
    store = domain_data(hass).auto_store
    assert store is not None
    return store.settings(entry_id)


# ------------------------------------------------------------------------ WebSocket

#: Home Assistant refuses a reused or non-integer request id, so every call gets a fresh one.
_REQUEST_IDS = itertools.count(1)


async def ws_call(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """One WebSocket request; the payload is given a fresh id (in place), and the reply is returned."""
    payload["id"] = next(_REQUEST_IDS)
    await client.send_json(payload)
    return await client.receive_json()


async def admin(hass: HomeAssistant, hass_ws_client):
    """An authenticated socket whose user is an administrator."""
    return await hass_ws_client(hass)


async def non_admin(hass: HomeAssistant, hass_ws_client, token: str):
    """An authenticated socket whose user is *not* an administrator."""
    return await hass_ws_client(hass, token)


# ------------------------------------------------- a measured site around one charger


def set_derived_site_entities(
    hass: HomeAssistant,
    prefix: str,
    watts: float,
    voltage: float = 230.0,
    per_phase_watts: dict[str, float] | None = None,
) -> dict[str, dict[str, str]]:
    """The three derived-mode power/reactive/voltage entities of a site.

    `watts` is one balanced value for all phases; `per_phase_watts` overrides single phases.
    """
    derived = {
        phase: {
            "power": f"sensor.{prefix}_power_{phase.lower()}",
            "reactive_power": f"sensor.{prefix}_reactive_{phase.lower()}",
            "voltage": f"sensor.{prefix}_voltage_{phase.lower()}",
        }
        for phase in PHASES
    }
    for phase in PHASES:
        phase_watts = (per_phase_watts or {}).get(phase, watts)
        hass.states.async_set(derived[phase]["power"], str(phase_watts), {"unit_of_measurement": "W"})
        hass.states.async_set(derived[phase]["reactive_power"], "0", {"unit_of_measurement": "var"})
        hass.states.async_set(derived[phase]["voltage"], str(voltage), {"unit_of_measurement": "V"})
    return derived


def set_site_power_w(hass: HomeAssistant, prefix: str, watts_per_phase: float) -> None:
    """Move every phase's signed active power by moving the sensors the site is derived from."""
    for phase in PHASES:
        hass.states.async_set(
            f"sensor.{prefix}_power_{phase.lower()}",
            str(watts_per_phase),
            {"unit_of_measurement": "W"},
        )


def set_site_current_a(hass: HomeAssistant, prefix: str, amps: float, voltage: float = 230.0) -> None:
    """Move the site's own current per phase (`sqrt(P^2+Q^2)/V`, Q held at 0)."""
    set_site_power_w(hass, prefix, amps * voltage)


def set_charger_delivered_a(hass: HomeAssistant, charger_prefix: str, amps: float) -> None:
    """A charger's own delivered current, per phase."""
    for phase in PHASES:
        set_current_sensor(hass, f"sensor.{charger_prefix}_{phase.lower()}", amps)


VOLTAGE_V = 230.0


class SecondsClock:
    """A hand-advanced fake clock for `SolarExecutionCoordinator._now` -- nothing here
    ever sleeps; `site/solar_surplus.py`'s own state machine only ever compares the numbers
    this yields, exactly as its own tests drive it (see `tests/test_solar_surplus.py`).
    """

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def now(self) -> float:
        return self.value


async def solar_setup(
    hass: HomeAssistant,
    *,
    entry_id: str = "solar",
    main_fuse_a: float = 25.0,
    charging_at_setup: bool = False,
    strategy: str = STRATEGY_SOLAR,
) -> tuple[object, object, object, SolarExecutionCoordinator, SecondsClock, list, list]:
    """One charger, set up *before* its site -- the common real/test ordering
    (`SolarExecutionCoordinator.async_start`'s own docstring) -- with its strategy already
    `solar` and a derived-mode site wired to measure it. Returns
    `(charger_entry, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls)`.

    `charging_at_setup=True` puts the charge-control switch "on" *before* the charger
    entry is even set up, for the restart-adoption tests: `ChargingController.charging`
    is a live read of that switch's state, so this is what "HA restarted while the
    charger was already charging" looks like from this integration's own point of view.

    `strategy` selects `solar` or `hybrid`: `SolarExecutionCoordinator` runs identically for both,
    and this is the one fixture that builds a real charger, a real derived-mode site and a real,
    ticking coordinator together.
    """
    prefix = f"{entry_id}_charger"
    hass.states.async_set(f"switch.{prefix}", "on" if charging_at_setup else "off")

    charger = make_entry(
        hass,
        entry_id=prefix,
        charge_control=f"switch.{prefix}",
        current_limit=None,
        webhook_id=f"webhook-{entry_id}",
        title="Solar charger",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    # *After* the charger entry is set up, not before: this integration forwards the
    # `switch` platform, so setup loads the core `switch` `EntityComponent`, which
    # registers its own `turn_on`/`turn_off` handlers -- a mock registered earlier would
    # be replaced by them. Same ordering `tests/helpers.py`'s own `make_two_chargers`
    # already documents and relies on.
    turn_on_calls = async_mock_service(hass, "switch", "turn_on")
    turn_off_calls = async_mock_service(hass, "switch", "turn_off")

    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(
        charger.entry_id, mutate=lambda s: replace(s, strategy=strategy)
    )

    # The fake clock is installed *before* the site entry is set up: the site's own setup
    # calls `async_rebind_solar_execution` (`_async_setup_site_entry`), which is this
    # coordinator's *first* real evaluation whenever the charger was already charging
    # (`charging_at_setup`) -- that is exactly the adoption this fixture exists to test,
    # and it must see the same clock every later tick in a test does, or `on_since`/
    # `disarming_since` would be anchored to the real `time.monotonic()` this coordinator
    # was built with by default, hopelessly out of range of the fake `now` values below.
    clock = SecondsClock(0.0)
    coordinator = hass.config_entries.async_get_entry(charger.entry_id).runtime_data.solar
    assert isinstance(coordinator, SolarExecutionCoordinator)
    coordinator._now = clock.now

    # No surplus, no draw, to start: every test below moves these explicitly.
    derived_entities = set_derived_site_entities(hass, f"{entry_id}_site", 0.0)
    set_charger_delivered_a(hass, prefix, 0.0)

    site_entry = make_site_entry(
        hass,
        entry_id=f"{entry_id}_site",
        main_fuse_a=main_fuse_a,
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                "min_current_a": 6.0,
                CONF_MEASURED_CURRENT_SOURCE: source_to_dict(
                    PhaseMeasurementSource(
                        kind="separate_entities",
                        entity_ids={p: f"sensor.{prefix}_{p.lower()}" for p in PHASES},
                    )
                ),
            }
        },
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived_entities,
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()

    controller: ChargingController = controller_of(hass, charger.entry_id)
    site_controller: SiteCapacityController = controller_of(hass, site_entry.entry_id)

    # Every test below drives the site with an explicit `_recompute()` call at a chosen
    # fake instant -- see the module docstring -- so the site's own timer and state
    # listener (which would otherwise fire on every individual sensor write above and
    # below, each with only part of a tick's readings changed) are cancelled, matching
    # `test_yield_stepping_wiring._yield_setup`'s own isolation.
    if site_controller._timer_cancel is not None:
        site_controller._timer_cancel()
        site_controller._timer_cancel = None
    if site_controller._state_listener_cancel is not None:
        site_controller._state_listener_cancel()
        site_controller._state_listener_cancel = None

    turn_on_calls.clear()
    turn_off_calls.clear()
    return charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls


async def arm_and_start(hass: HomeAssistant, site_controller: SiteCapacityController, clock: SecondsClock) -> None:
    """Drive a fresh `solar_setup` site straight to `on` -- the shared first half of
    `test_surplus_rises_starts_a_short_cloud_survives_a_lasting_drop_stops`'s own replay."""
    set_site_power_w(hass, "solar_site", -1400.0)
    await tick_site(hass, site_controller)
    clock.value = 125.0
    await tick_site(hass, site_controller)


async def tick_site(hass: HomeAssistant, site_controller: SiteCapacityController) -> None:
    """One explicit site recompute, plus everything it schedules."""
    site_controller._recompute()
    await hass.async_block_till_done()


CAPACITY = 77.0


REGISTER = "sensor.wallbox_energy_register"


def add_car(hass: HomeAssistant, name: str, *, percent: str = "55") -> str:
    """A second vehicle-shaped device (one percent battery sensor and a range signal)."""
    MockConfigEntry(domain="test", entry_id=f"{name}_entry").add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=f"{name}_entry", identifiers={("test", name)}, name=name
    )
    add_percent_battery_sensor(hass, device.id, object_id=f"{name}_battery", percent=percent)
    range_entry = er.async_get(hass).async_get_or_create(
        "sensor", "test", f"{name}_range", device_id=device.id, suggested_object_id=f"{name}_range"
    )
    hass.states.async_set(
        range_entry.entity_id, "200", {"device_class": "distance", "unit_of_measurement": "km"}
    )
    return device.id


async def charger_and_car(
    hass: HomeAssistant, *, capacity: float | None = CAPACITY, soc_percent: str = "40", **settings: Any
) -> tuple[Any, str, str]:
    """A real charger entry with an energy register, and a vehicle device with one state of
    charge sensor (a cloud poll, at `soc_percent`), a range signal and, optionally, a remembered pack size."""
    hass.states.async_set("switch.wallbox", "off")
    charger = make_entry(
        hass, entry_id="soc_charger", charge_control="switch.wallbox", current_limit=None,
        webhook_id="webhook-soc", title="Wallbox",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(
        REGISTER, "1000.0",
        {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    )
    controller_of(hass, charger.entry_id).energy_register_entity_id = REGISTER

    MockConfigEntry(domain="test", entry_id="car_entry").add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id="car_entry", identifiers={("test", "ev6")}, name="EV6"
    )
    soc = add_percent_battery_sensor(hass, device.id, object_id="ev6_battery", percent=soc_percent)
    range_entry = er.async_get(hass).async_get_or_create(
        "sensor", "test", "ev6_range", device_id=device.id, suggested_object_id="ev6_range"
    )
    hass.states.async_set(range_entry.entity_id, "200", {"device_class": "distance", "unit_of_measurement": "km"})
    if capacity is not None:
        await vehicle_properties.async_update_vehicle_properties(
            hass, domain_data(hass).decision_store, device.id, {vehicle_properties.KEY_CAPACITY: capacity}
        )
    settings.setdefault("driver", DRIVER_TARGET_SOC)
    settings.setdefault("target", TargetSocIntent(vehicle_id=device.id, target_percent=80))
    await go_auto(hass, charger.entry_id, **settings)
    return charger, device.id, soc


async def real_controller_stop(hass: HomeAssistant, frozen: Any) -> None:
    charger, device_id, _ = await charger_and_car(hass)
    controller = controller_of(hass, charger.entry_id)
    turn_off = async_mock_service(hass, "homeassistant", "turn_off")
    async_mock_service(hass, "homeassistant", "turn_on")
    start, end = future_window()
    await controller.async_install(
        ChargingPlan(start=start, end=end, amps=16, phases=3, target_soc_percent=80.0, vehicle_id=device_id)
    )
    assert controller.target_stop_record is None
    # The last poll is half an hour old by the time the meter moves: no longer a fresh reading.
    frozen.tick(timedelta(minutes=30))

    def meter(kwh: float) -> None:
        hass.states.async_set(
            REGISTER, str(1000.0 + kwh),
            {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
        )

    # 40 % + 81 % - 40 % = 41 points of 77 kWh at 0.9: 35.08 kWh from the wall.
    meter(34.0)
    await hass.async_block_till_done()
    assert controller.plan is not None and not turn_off, "80.9 % estimated is below target plus margin"
    meter(35.2)
    await hass.async_block_till_done()
    assert controller.plan is None
    record = controller.target_stop_record
    assert record is not None and record["basis"] == "estimate" and record["estimated"] is True
    assert record["soc_percent"] == pytest.approx(81.0, abs=0.3)


async def one_charger(hass: HomeAssistant):
    hass.states.async_set("switch.charger_a", "off")
    entry = make_entry(
        hass,
        entry_id="entry_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def vehicle_device(
    hass: HomeAssistant,
    *,
    unique_id: str,
    name: str,
    limits: tuple[tuple[str, str], ...] = (("charge_limit", "90"),),
) -> dict[str, Any]:
    """A vehicle-shaped device, with the charge-limit numbers a test needs.

    `limits` is `(object_id, live value)` pairs -- the shape the resolver has to
    choose *between*, so a test can hand it two limits, or none that reports a
    value. Returns the device id, the state-of-charge and range entity ids a
    vehicle needs to be vehicle-shaped at all, and the limit entity ids in the
    order they were created (which is deliberately not the order the resolver
    considers them in: entities are ordered by entity id).
    """
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=plain_config_entry(hass),
        identifiers={("test", unique_id)},
        name=name,
    )
    return {
        "device_id": device.id,
        "soc": vehicle_entity(
            hass,
            device_id=device.id,
            domain="sensor",
            object_id="battery_level",
            state="55",
            attributes={"device_class": "battery", "unit_of_measurement": "%"},
        ),
        "range": vehicle_entity(
            hass,
            device_id=device.id,
            domain="sensor",
            object_id="range",
            state="310",
            attributes={"device_class": "distance", "unit_of_measurement": "km"},
        ),
        "limits": [
            vehicle_entity(
                hass,
                device_id=device.id,
                domain="number",
                object_id=object_id,
                state=state,
                attributes={"unit_of_measurement": "%", "min": 50, "max": 100},
            )
            for object_id, state in limits
        ],
    }


def forecast_entry(hass: HomeAssistant, *, entry_id: str, title: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain="roof", entry_id=entry_id, title=title)
    entry.add_to_hass(hass)
    return entry


async def setup_site_with_charger(
    hass: HomeAssistant,
    *,
    charger_entry_id: str = "entry_a",
    site_entry_id: str = "site_a",
    **site_kwargs: Any,
) -> tuple[MockConfigEntry, MockConfigEntry]:
    """One real charger and one real site that lists it, both set up through Home Assistant.

    The charge-control entity and the webhook id are derived from `charger_entry_id` (not a fixed
    "switch.charger_a"/"webhook-a") so a test that sets up more than one charger in one `hass` --
    the fixture-writing round trip among them -- never collides on either.
    """
    charge_control = f"switch.{charger_entry_id}"
    hass.states.async_set(charge_control, "off")
    charger = make_entry(
        hass,
        entry_id=charger_entry_id,
        charge_control=charge_control,
        current_limit=None,
        webhook_id=f"webhook-{charger_entry_id}",
        title=charger_entry_id,
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    site = make_site_entry(
        hass, entry_id=site_entry_id, charger_entry_ids=[charger_entry_id], **site_kwargs
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    return charger, site


CPID = "picasso"


def ocpp_entity(
    hass: HomeAssistant,
    *,
    owner: MockConfigEntry,
    device_id: str,
    cpid: str,
    domain: str,
    key: str,
    connector: int | None = None,
    state: str | None = None,
    attributes: dict | None = None,
) -> str:
    """One OCPP entity, with the entity-id *and* unique-id shapes the integration emits."""
    object_id = (
        f"{cpid}_{key}" if connector is None else f"{cpid}_connector_{connector}_{key}"
    )
    unique_id = (
        f"{domain}.ocpp.{cpid}.{key}"
        if connector is None
        else f"{domain}.ocpp.{cpid}.conn{connector}.{key}"
    )
    entry = er.async_get(hass).async_get_or_create(
        domain,
        "ocpp",
        unique_id,
        device_id=device_id,
        config_entry=owner,
        suggested_object_id=object_id,
    )
    if state is not None:
        hass.states.async_set(entry.entity_id, state, dict(attributes or {}))
    return entry.entity_id


def station_device(hass: HomeAssistant, owner: MockConfigEntry, cpid: str) -> str:
    """The charge point's station device, registered the way 0.12 registers it."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("ocpp", cpid)},
        name=f"{cpid} station",
    )
    return device.id


def two_connector_charger(hass: HomeAssistant, owner: MockConfigEntry) -> dict[str, str]:
    """A two-connector 0.12 charge point: station ceiling, both charge controls, both session limits."""
    device_id = station_device(hass, owner, CPID)
    shared = {"owner": owner, "device_id": device_id, "cpid": CPID}
    return {
        "station": ocpp_entity(
            hass,
            **shared,
            domain="number",
            key="maximum_current",
            state="16",
            attributes={"unit_of_measurement": "A", "min": 0, "max": 16},
        ),
        "charge_control_1": ocpp_entity(
            hass, **shared, domain="switch", key="charge_control", connector=1, state="off"
        ),
        "charge_control_2": ocpp_entity(
            hass, **shared, domain="switch", key="charge_control", connector=2, state="off"
        ),
        # 0.12 reports an unavailable session limit outside a transaction; it must still be discovered.
        "session_1": ocpp_entity(
            hass,
            **shared,
            domain="number",
            key="session_current_limit",
            connector=1,
            state="unavailable",
            attributes=AMPERE,
        ),
        "session_2": ocpp_entity(
            hass,
            **shared,
            domain="number",
            key="session_current_limit",
            connector=2,
            state="unavailable",
            attributes=AMPERE,
        ),
    }


def vehicle_entity(
    hass: HomeAssistant,
    *,
    device_id: str,
    domain: str,
    object_id: str,
    state: str,
    attributes: dict | None = None,
) -> str:
    entry = er.async_get(hass).async_get_or_create(
        domain,
        "test",
        f"{device_id}_{object_id}",
        device_id=device_id,
        suggested_object_id=object_id,
    )
    hass.states.async_set(entry.entity_id, state, attributes or {})
    return entry.entity_id


def plain_config_entry(hass: HomeAssistant) -> str:
    """One throwaway config entry, so test-owned devices can be registered the
    way an EV integration's own devices are."""
    entry_id = "refresh_vehicle_devices"
    if hass.config_entries.async_get_entry(entry_id) is None:
        MockConfigEntry(domain="test", entry_id=entry_id).add_to_hass(hass)
    return entry_id


async def setup_charger_and_site(
    hass: HomeAssistant, entry_id: str = "entry_a", *, site: bool = True, **site_kwargs: Any
) -> tuple[MockConfigEntry, MockConfigEntry | None]:
    control = register(hass, "switch", f"{entry_id}_control", f"Control {entry_id}")
    charger = make_entry(
        hass,
        entry_id=entry_id,
        charge_control=control,
        current_limit=None,
        webhook_id=f"webhook-{entry_id}",
        title=entry_id,
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    site_entry = None
    if site:
        site_entry = make_site_entry(
            hass, entry_id=f"site_{entry_id}", charger_entry_ids=[entry_id], **site_kwargs
        )
        assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    return charger, site_entry



async def measured_site_world(
    hass: HomeAssistant,
    name: str,
    *,
    title: str | None = None,
    start_amps: int = 9,
    delivered_a: float = 10,
    site_watts: float = 3000.0,
    main_fuse_a: float = 20.0,
    safety_margin_a: float = 1.0,
    **site_kwargs: Any,
):
    """One charging 3-phase charger and a derived-mode site measuring it, both really set up.

    Returns `(charger_entry, charger_controller, site_entry, site_controller, derived_entities)`.
    Every entity id is derived from `name`: `switch.<name>_charger`, `sensor.<name>_charger_l1..3`
    for the charger's delivered current and `sensor.<name>_site_*` for the site's derived power.
    """
    charge_control = f"switch.{name}_charger"
    hass.states.async_set(charge_control, "off")
    async_mock_service(hass, "switch", "turn_on")
    charger = make_entry(
        hass,
        entry_id=f"charger_{name}",
        charge_control=charge_control,
        current_limit=None,
        webhook_id=f"webhook-{name}",
        title=title or name.capitalize(),
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    charger_controller = controller_of(hass, charger.entry_id)
    await charger_controller.async_start(amps=start_amps)
    hass.states.async_set(charge_control, "on")
    set_charger_delivered_a(hass, f"{name}_charger", delivered_a)

    derived_entities = set_derived_site_entities(hass, f"{name}_site", site_watts)
    site_kwargs.setdefault("phase_wiring", {
        charger.entry_id: {
            "phases": 3,
            "phase": None,
            CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                *(f"sensor.{name}_charger_l{n}" for n in (1, 2, 3))
            ),
        }
    })
    site = make_site_entry(
        hass,
        entry_id=f"site_{name}",
        main_fuse_a=main_fuse_a,
        safety_margin_a=safety_margin_a,
        charger_entry_ids=[charger.entry_id],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived_entities,
        **site_kwargs,
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    return charger, charger_controller, site, controller_of(hass, site.entry_id), derived_entities


def separate_entities_source(l1: str, l2: str, l3: str) -> dict[str, Any]:
    """A charger's measured-current source of three separate per-phase current entities."""
    return source_to_dict(
        PhaseMeasurementSource(kind="separate_entities", entity_ids={"L1": l1, "L2": l2, "L3": l3})
    )
