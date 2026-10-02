"""A dumb charger behind a smart plug: the plug's power sensor is accepted where an energy sensor was,
SpotNav integrates it to energy itself, and a charger with no status sensor is read as not drawing from it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CHARGING_STATE,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_POWER_ENTITY,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    MODE_GENERIC,
)
from custom_components.spotnav.execution.charge_progress import (
    ChargeProgressFacts,
    POWER_GRACE_PERIOD_S,
    STATE_NORMAL,
    STATE_UNKNOWN,
    STATE_VEHICLE_NOT_REQUESTING_CURRENT,
    INSTRUCTION_PUBLISH,
    INSTRUCTION_START,
    observe,
)
from custom_components.spotnav.execution.power_energy import PowerIntegrator, integrated_energy_unique_id, power_w_of

from .helpers import make_entry
from .messages import register, update_entity_config_message
from .world import admin, controller_of, ws_call

T0 = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)


def power_state(value: str, unit: str = "W", device_class: str = "power") -> State:
    return State("sensor.plug_power", value, {"device_class": device_class, "unit_of_measurement": unit})


# ---- reading and integrating the power


def test_power_is_read_in_watts_from_a_power_sensor_only() -> None:
    assert power_w_of(power_state("1500")) == 1500.0
    assert power_w_of(power_state("2.3", "kW")) == 2300.0
    # Export is not the charger drawing; a wrong class or unit, or no number, is not power at all.
    assert power_w_of(power_state("-40")) == 0.0
    assert power_w_of(power_state("5", "kWh", "energy")) is None
    assert power_w_of(power_state("5", "A")) is None
    assert power_w_of(power_state("unavailable")) is None
    assert power_w_of(power_state("nope")) is None
    assert power_w_of(None) is None


def test_the_trapezoid_integrates_fresh_samples_and_leaves_gaps_alone() -> None:
    integrator = PowerIntegrator(max_age_s=120.0)
    integrator.sample(T0, 0.0)
    integrator.sample(T0 + timedelta(seconds=60), 2000.0)
    # (0 + 2000) / 2 W for 60 s.
    assert abs(integrator.total_kwh - 1000 * 60 / 3_600_000) < 1e-9
    integrator.sample(T0 + timedelta(seconds=120), 2000.0)
    assert abs(integrator.total_kwh - (1000 * 60 + 2000 * 60) / 3_600_000) < 1e-9
    before = integrator.total_kwh
    # Silence longer than the maximum age is not integrated, in either direction.
    integrator.sample(T0 + timedelta(seconds=120 + 600), 2000.0)
    assert integrator.total_kwh == before
    # An unreadable sample ends the run: nothing is integrated across it.
    integrator.sample(T0 + timedelta(seconds=120 + 660), None)
    integrator.sample(T0 + timedelta(seconds=120 + 700), 2000.0)
    assert integrator.total_kwh == before
    integrator.sample(T0 + timedelta(seconds=120 + 760), 2000.0)
    assert abs(integrator.total_kwh - before - 2000 * 60 / 3_600_000) < 1e-9


# ---- the not-drawing observation


def facts(power_w: float | None, *, expected: bool = True) -> ChargeProgressFacts:
    return ChargeProgressFacts(
        expected=expected,
        start_pending=False,
        connector_status=None,
        current_import_a=None,
        subject="s",
        power_mode=True,
        power_w=power_w,
        idle_power_w=100.0,
    )


def test_power_under_the_threshold_for_five_minutes_in_a_window_says_the_car_is_not_drawing() -> None:
    started = observe(facts(40.0), None, T0)
    assert started.instruction == INSTRUCTION_START
    assert (started.progress.state, started.progress.reason) == (STATE_NORMAL, "power_below_threshold_pending")
    # Four minutes in: still waiting, the advisory is not published early.
    early = observe(facts(40.0), T0, T0 + timedelta(seconds=POWER_GRACE_PERIOD_S - 60))
    assert early.progress.state == STATE_NORMAL
    done = observe(facts(40.0), T0, T0 + timedelta(seconds=POWER_GRACE_PERIOD_S))
    assert done.instruction == INSTRUCTION_PUBLISH
    assert (done.progress.state, done.progress.reason) == (STATE_VEHICLE_NOT_REQUESTING_CURRENT, "power_below_threshold")
    assert POWER_GRACE_PERIOD_S == 300.0


def test_power_at_the_threshold_or_outside_a_window_or_unreadable_says_nothing_wrong() -> None:
    assert observe(facts(100.0), T0, T0).progress.reason == "power_flowing"
    assert observe(facts(2300.0), T0, T0).progress.state == STATE_NORMAL
    assert observe(facts(5.0, expected=False), T0, T0).progress.reason == "charge_not_expected"
    unreadable = observe(facts(None), T0, T0).progress
    assert (unreadable.state, unreadable.reason) == (STATE_UNKNOWN, "power_unavailable")


# ---- the charger


async def _plug_charger(hass: HomeAssistant, name: str = "plug", **extra):
    control = register(hass, "switch", f"{name}_control", f"Control {name}")
    power = f"sensor.{name}_power"
    hass.states.async_set(power, "0", {"device_class": "power", "unit_of_measurement": "W"})
    entry = make_entry(
        hass,
        entry_id=f"entry_{name}",
        charge_control=control,
        current_limit=None,
        webhook_id=f"webhook-{name}",
        title=name,
        extra={CONF_POWER_ENTITY: power, **extra},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, power


async def test_a_charger_without_a_status_sensor_is_judged_by_its_power(hass: HomeAssistant) -> None:
    entry, power = await _plug_charger(hass)
    controller = controller_of(hass, entry.entry_id)
    hass.states.async_set(power, "35", {"device_class": "power", "unit_of_measurement": "W"})
    seen = controller.charge_progress_facts()
    assert (seen.power_mode, seen.power_w, seen.idle_power_w) == (True, 35.0, 100.0)

    with_status, _ = await _plug_charger(
        hass, "plug_status", **{CONF_CHARGING_STATE: {"entity_id": "sensor.plug_status", "charging_values": ["on"]}}
    )
    assert controller_of(hass, with_status.entry_id).charge_progress_facts().power_mode is False


async def test_the_integrated_energy_stands_in_for_the_register_and_follows_the_power(
    hass: HomeAssistant, freezer
) -> None:
    entry, power = await _plug_charger(hass)
    controller = controller_of(hass, entry.entry_id)
    registry = er.async_get(hass)
    energy_id = registry.async_get_entity_id("sensor", DOMAIN, integrated_energy_unique_id(entry.entry_id))
    assert energy_id is not None
    assert controller.energy_register_entity_id == energy_id
    assert controller.adapter.capabilities.reads_energy_register is True

    attributes = {"device_class": "power", "unit_of_measurement": "W"}
    hass.states.async_set(power, "2000", attributes)
    await hass.async_block_till_done()
    freezer.tick(60)
    hass.states.async_set(power, "2000", attributes, force_update=True)
    await hass.async_block_till_done()
    state = hass.states.get(energy_id)
    assert state is not None
    assert state.attributes["device_class"] == "energy" and state.attributes["state_class"] == "total_increasing"
    # 2000 W for the 60 s that were seen.
    assert abs(float(state.state) - 2000 * 60 / 3_600_000) < 1e-5


async def test_an_energy_register_of_the_persons_own_is_kept_and_no_integrated_sensor_is_made(
    hass: HomeAssistant,
) -> None:
    register_id = register(hass, "sensor", "own_register", device_class="energy")
    entry, _ = await _plug_charger(hass, "plug_own", energy_register_entity=register_id)
    assert controller_of(hass, entry.entry_id).energy_register_entity_id == register_id
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, integrated_energy_unique_id(entry.entry_id)) is None


async def test_the_manual_flow_takes_a_power_sensor(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.plug_switch", "off")
    hass.states.async_set("sensor.plug_flow_power", "0", {"device_class": "power", "unit_of_measurement": "W"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_MODE: MODE_GENERIC})
    assert result["step_id"] == "generic"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGE_CONTROL: "switch.plug_switch", CONF_POWER_ENTITY: "sensor.plug_flow_power"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POWER_ENTITY] == "sensor.plug_flow_power"


async def test_the_charger_dialog_sets_and_clears_the_power_sensor(hass: HomeAssistant, hass_ws_client) -> None:
    entry, power = await _plug_charger(hass, "plug_dialog")
    client = await admin(hass, hass_ws_client)
    other = register(hass, "sensor", "another_plug_power", device_class="power")
    ok = (
        await ws_call(client, update_entity_config_message(entry.entry_id, scope="charger", changes={"power_entity": other}))
    )["result"]
    assert ok["ok"] is True, ok
    assert hass.config_entries.async_get_entry(entry.entry_id).data[CONF_POWER_ENTITY] == other
    cleared = (
        await ws_call(client, update_entity_config_message(entry.entry_id, scope="charger", changes={"power_entity": ""}))
    )["result"]
    assert cleared["ok"] is True, cleared
    assert CONF_POWER_ENTITY not in hass.config_entries.async_get_entry(entry.entry_id).data
