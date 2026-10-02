"""Onboarding bug hunt 2026-10-03: each test pins one real bug that has since been fixed.

The BH-n ids are the entries in plans/bughunt_2026-10-03.md.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import CONF_CHARGE_CONTROL
from custom_components.spotnav.execution.controller import ChargingController

from .helpers import install_schedule


def _window_ahead() -> dict:
    start = dt_util.utcnow() + timedelta(hours=2)
    return {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(), "amps": 10}


async def test_a_charge_already_running_when_the_charger_loads_late_is_not_held(hass: HomeAssistant) -> None:
    # Home Assistant restarts mid-charge; the charger's integration loads after SpotNav.
    hass.states.async_set("switch.a", "unavailable")
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await controller.async_initialize()
    await install_schedule(controller, _window_ahead())
    stops = async_mock_service(hass, "switch", "turn_off")

    hass.states.async_set("switch.a", "on")  # the integration loaded: it was charging all along
    await hass.async_block_till_done()

    await controller.async_shutdown()
    assert stops == []


async def test_a_safety_margin_at_or_above_the_main_fuse_is_refused(hass: HomeAssistant) -> None:
    from homeassistant.data_entry_flow import FlowResultType, InvalidData

    from custom_components.spotnav.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    try:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "name": "Home",
                "main_fuse_a": 20,
                "safety_margin_a": 25,
                "measurement_mode": "direct_phase_current",
                "charger_entry_ids": [],
            },
        )
    except InvalidData:
        return  # refused by the schema: fine
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "site"
    assert result["errors"] == {"safety_margin_a": "safety_margin_at_or_above_fuse"}


async def test_a_charger_whose_charge_control_entity_is_gone_is_reported_unavailable(
    hass: HomeAssistant, offline_relay
) -> None:
    from custom_components.spotnav.api.dashboard import capture_charger

    from .world import setup_charger

    entry = await setup_charger(hass)
    assert capture_charger(hass, entry).available is True

    hass.states.async_remove("switch.charger_a")  # the entity was renamed (the registry id changed)
    await hass.async_block_till_done()

    assert capture_charger(hass, entry).available is False


async def test_a_charger_command_that_never_answers_is_given_up_on(hass: HomeAssistant) -> None:
    import asyncio

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    hass.states.async_set("switch.a", "off")
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await controller.async_initialize()
    never = asyncio.Event()

    async def hung(_call) -> None:
        await never.wait()

    hass.services.async_register("switch", "turn_on", hung)
    task = hass.async_create_task(controller.async_start(manual=True))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Ten minutes later the command has been waiting far longer than any cloud answer takes.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=10))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gave_up = task.done()
    results = await asyncio.gather(task, return_exceptions=True) if gave_up else []
    if gave_up:
        assert not controller._lock.locked()
        last = controller.adapter.command_log()[-1]
        assert last["result"] == "error" and last["error"]["type"] == "TimeoutError"
        del results

    never.set()  # let the stuck call end so the test can clean up either way
    await asyncio.gather(task, return_exceptions=True)
    await controller.async_shutdown()
    assert gave_up


async def test_the_area_is_suggested_once_the_relay_answers_after_a_first_run_without_it(hass: HomeAssistant) -> None:
    from .test_first_run import _Controller, _Manager, _setup, CATALOGUE
    from custom_components.spotnav.planning.first_run import async_seed_first_run
    from custom_components.spotnav.runtime import domain_data

    store, entry = await _setup(hass, catalogue=None)  # the relay was unreachable at setup
    await async_seed_first_run(hass, entry, _Controller(None), None)
    assert store.settings(entry.entry_id).area_id is None

    domain_data(hass).price_refresh = _Manager(CATALOGUE)  # the next start, the relay answers
    await async_seed_first_run(hass, entry, _Controller(None), None)

    assert store.settings(entry.entry_id).area_id == "SE1"


def test_an_unreadable_control_is_no_observation_and_the_first_readable_state_is_the_baseline() -> None:
    from custom_components.spotnav.execution.window_hold import HOLD, NOTHING, WindowHold

    hold = WindowHold()
    hold.baseline(None)
    assert hold.observe(control_on=None, connected=None, gap=True) == NOTHING
    assert hold.observe(control_on=True, connected=None, gap=True) == NOTHING  # baseline, not a start
    assert hold.observe(control_on=False, connected=None, gap=True) == NOTHING
    assert hold.observe(control_on=None, connected=None, gap=True) == NOTHING
    assert hold.observe(control_on=True, connected=None, gap=True) == HOLD  # a real start


async def test_an_area_a_person_cleared_is_not_suggested_again(hass: HomeAssistant) -> None:
    from .test_first_run import _Controller, _Manager, _setup, CATALOGUE
    from custom_components.spotnav.planning.auto_settings import AutoSettings
    from custom_components.spotnav.planning.first_run import async_seed_first_run
    from custom_components.spotnav.runtime import domain_data

    store, entry = await _setup(hass, catalogue=None)
    await async_seed_first_run(hass, entry, _Controller(None), None)
    await store.async_update(entry.entry_id, mutate=lambda current: AutoSettings(area_id=None), confirm=True)

    domain_data(hass).price_refresh = _Manager(CATALOGUE)
    assert not await async_seed_first_run(hass, entry, _Controller(None), None)
    assert store.settings(entry.entry_id).area_id is None


async def test_a_disabled_or_removed_charge_control_says_which_and_names_the_entity(
    hass: HomeAssistant, offline_relay
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.spotnav.api.dashboard import capture_charger

    from .world import setup_charger

    registry = er.async_get(hass)
    registry.async_get_or_create("switch", "demo", "ctl", suggested_object_id="garage")
    entry = await setup_charger(hass, charge_control="switch.garage")
    assert capture_charger(hass, entry).problem is None

    registry.async_update_entity("switch.garage", disabled_by=er.RegistryEntryDisabler.USER)
    captured = capture_charger(hass, entry)
    assert (captured.available, captured.problem, captured.problem_entity) == (
        False,
        "control_disabled",
        "switch.garage",
    )

    registry.async_update_entity("switch.garage", disabled_by=None)
    hass.states.async_remove("switch.garage")
    assert capture_charger(hass, entry).problem == "control_missing"

    hass.states.async_set("switch.garage", "unavailable")  # unavailable is not gone
    assert capture_charger(hass, entry).problem is None


async def test_renaming_the_charge_control_follows_the_new_entity_id(hass: HomeAssistant, offline_relay) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.spotnav.runtime import charger_data

    from .world import setup_charger

    registry = er.async_get(hass)
    registry.async_get_or_create("switch", "demo", "ctl", suggested_object_id="garage")
    entry = await setup_charger(hass, charge_control="switch.garage")

    registry.async_update_entity("switch.garage", new_entity_id="switch.garage_box")
    await hass.async_block_till_done()
    hass.states.async_set("switch.garage_box", "off")

    assert entry.data[CONF_CHARGE_CONTROL] == "switch.garage_box"
    controller = hass.config_entries.async_get_entry(entry.entry_id).runtime_data.controller
    assert controller.charge_control == "switch.garage_box"
    assert charger_data(hass, entry.entry_id) is not None


def test_replacing_an_entity_id_walks_nested_values_and_never_keys() -> None:
    from custom_components.spotnav.entity_renames import replace_entity_id

    value = {"a": "sensor.x", "b": {"sensor.x": ["sensor.x", "sensor.y"]}, "n": 3}
    result, changed = replace_entity_id(value, "sensor.x", "sensor.z")
    assert changed
    assert result == {"a": "sensor.z", "b": {"sensor.x": ["sensor.z", "sensor.y"]}, "n": 3}
    assert replace_entity_id(value, "sensor.q", "sensor.z") == (value, False)
