"""The Auto entity surface: real platforms, real services, real config entries.

Every test here goes through Home Assistant's own entity platforms and services -- the entries
are set up through `hass.config_entries.async_setup`, values are written with
`select.select_option`/`number.set_value`/`switch.turn_on`/`time.set_value`/`button.press`, and
what is asserted is what a person or an automation would see: entity states, attributes and
the plan and settings the reviewed backend ends up holding.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import replace
from datetime import time, timezone
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution.controller import (
    ABSOLUTE_MAX_AMPS,
    ABSOLUTE_MIN_AMPS,
)


from .relay import DE_LU, SE4, serve, serve_index

#: The fixture catalogue's other markets, by id: NO1 publishes a literal zero fee, DK1
#: publishes no grid fee at all, and DE-LU publishes no suggestion for anything.
NO1 = "NO1"
DK1 = "DK1"
from .relay import StubTransport
from .world import call, entity_id, go_auto, settings_of, setup_charger, setup_site
from .world import controller_of
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.runtime import executor_for, preview_for

pytestmark = pytest.mark.usefixtures("offline_relay")



# ---------------------------------------------------- the surface exists, and is quiet


AUTO_SENSOR_KEYS = (
    "auto_plan_state",
    "auto_execution_state",
    "auto_next_start",
    "auto_next_end",
    "auto_periods",
    "auto_cost",
    "auto_energy",
    "auto_settings_revision",
)


def registered(hass: HomeAssistant, entry_id: str) -> set[str]:
    """Every unique id this entry registered, whatever platform it belongs to."""
    registry = er.async_get(hass)
    return {
        entry.unique_id
        for entry in er.async_entries_for_config_entry(registry, entry_id)
    }


def device_of(hass: HomeAssistant, entry_id: str) -> str:
    """The device every one of this entry's entities belongs to."""
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    entity = er.async_entries_for_config_entry(registry, entry_id)[0]
    assert entity.device_id is not None
    device = devices.async_get(entity.device_id)
    assert device is not None
    return device.id


async def test_an_incomplete_charger_gets_the_whole_surface_and_does_nothing(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """A charger that never chose Auto gets its entities, all of them quiet.

    The entities exist so that "Auto is not configured here" is visible rather than absent,
    and nothing about setting this up may touch the charger: no plan, no service call and no
    stored setting of Auto's.
    """
    turn_on = async_mock_service(hass, "switch", "turn_on")
    turn_off = async_mock_service(hass, "switch", "turn_off")
    await setup_charger(hass)

    unique_ids = registered(hass, "entry_a")
    for key in AUTO_SENSOR_KEYS:
        assert f"entry_a_{key}" in unique_ids
    for key in ("price_area", "charging_phases", "fiscal_vat_policy"):
        assert f"entry_a_{key}" in unique_ids
    for key in ("recalculate_auto", "pause_auto", "resume_auto"):
        assert f"entry_a_{key}" in unique_ids

    state = hass.states.get(entity_id(hass, "entry_a", "auto_plan_state"))
    assert state.state == "incomplete_settings"
    execution = hass.states.get(entity_id(hass, "entry_a", "auto_execution_state"))
    assert execution.state == "not_applied" and execution.attributes["paused"] is False
    controller = controller_of(hass, "entry_a")
    assert controller.plan is None
    assert turn_on == [] and turn_off == []
    assert settings_of(hass, "entry_a").revision == 0


async def test_a_site_entry_never_grows_a_chargers_auto_entities(hass: HomeAssistant) -> None:
    """A site has its own, smaller platform list, and none of it is Auto."""
    entry = await setup_site(hass)

    unique_ids = registered(hass, entry.entry_id)
    assert unique_ids, "a site still registers its own entities"
    assert not [uid for uid in unique_ids if "auto_" in uid or "fiscal" in uid]
    for key in ("planning_mode", "price_area", "charging_phases"):
        assert f"{entry.entry_id}_{key}" not in unique_ids


async def test_unique_ids_and_device_grouping_survive_a_reload_and_a_rename(
    hass: HomeAssistant,
) -> None:
    """Ids are keyed by the entry id, and the device survives both operations."""
    entry = await setup_charger(hass, title="Garage")
    before_ids = registered(hass, entry.entry_id)
    before_device = device_of(hass, entry.entry_id)

    hass.config_entries.async_update_entry(entry, title="Carport")
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert registered(hass, entry.entry_id) == before_ids
    assert device_of(hass, entry.entry_id) == before_device
    assert hass.states.get(entity_id(hass, "entry_a", "auto_plan_state")).state == "incomplete_settings"


async def test_two_chargers_keep_their_values_and_actions_apart(hass: HomeAssistant) -> None:
    """One charger's edit is never another charger's state."""
    await setup_charger(hass, entry_id="entry_a", charge_control="switch.charger_a")
    await setup_charger(
        hass,
        entry_id="entry_b",
        charge_control="switch.charger_b",
        webhook_id="webhook-b",
    )

    await call(
        hass,
        "select",
        "select_option",
        {
            "entity_id": entity_id(hass, "entry_a", "price_area", "select"),
            "option": SE4,
        },
    )
    await call(
        hass,
        "number",
        "set_value",
        {"entity_id": entity_id(hass, "entry_a", "charging_current", "number"), "value": 13},
    )

    assert settings_of(hass, "entry_a").amps == 13
    assert settings_of(hass, "entry_b").amps is None
    assert settings_of(hass, "entry_b").revision == 0
    assert hass.states.get(entity_id(hass, "entry_b", "charging_current", "number")).state == "unknown"

@freeze_time("2026-09-22 06:00:00")
async def test_completing_settings_enables_auto_and_installs_once(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """The mode select is the one act that turns automation on, and it installs one plan."""
    turn_on = async_mock_service(hass, "switch", "turn_on")
    entry = await setup_charger(hass)
    preview = preview_for(hass, entry.entry_id)
    assert preview is not None and preview.snapshot().state == "incomplete_settings"
    await call(
        hass,
        "select",
        "select_option",
        {"entity_id": entity_id(hass, entry.entry_id, "price_area", "select"), "option": SE4},
    )
    await call(
        hass,
        "number",
        "set_value",
        {"entity_id": entity_id(hass, entry.entry_id, "charging_current", "number"), "value": 10},
    )
    await call(
        hass,
        "select",
        "select_option",
        {
            "entity_id": entity_id(hass, entry.entry_id, "charging_phases", "select"),
            "option": "1",
        },
    )
    # No deadline: the whole point of a manual-kWh plan is that it may use the hours it likes.
    await call(
        hass,
        "switch",
        "turn_off",
        {"entity_id": entity_id(hass, entry.entry_id, "departure_enabled", "switch")},
    )

    controller = controller_of(hass, entry.entry_id)
    assert controller.plan is not None, "a complete setup plans a charge"
    executor = executor_for(hass, entry.entry_id)
    assert executor is not None and executor.applied is not None
    assert executor.execution_state() in ("scheduled", "active")
    assert len(turn_on) <= 1, "the charger is brought to the plan, not repeatedly"

    state = hass.states.get(entity_id(hass, entry.entry_id, "auto_plan_state"))
    assert state is not None and state.state in ("proposal_ready", "proposal_unpriced")
    assert state.attributes["applied"] is True
    assert state.attributes["area_id"] == SE4
    execution = hass.states.get(entity_id(hass, entry.entry_id, "auto_execution_state"))
    assert execution is not None and execution.attributes["applied_identity"] is not None


async def test_choosing_auto_incompletely_is_safe_and_names_what_is_missing(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Auto without its inputs stays incomplete, installs nothing, and says what is absent."""
    turn_on = async_mock_service(hass, "switch", "turn_on")
    entry = await setup_charger(hass)

    assert controller_of(hass, entry.entry_id).plan is None
    assert turn_on == []
    state = hass.states.get(entity_id(hass, entry.entry_id, "auto_plan_state"))
    assert state is not None and state.state == "incomplete_settings"
    assert state.attributes["reason"] == "settings_missing"
    assert list(state.attributes["missing"]) == ["area", "phases", "amps"]


async def test_a_write_carries_the_revision_it_displayed_and_loses_no_edit(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """A concurrent edit wins the race: the entity's write is refused, not blind."""
    entry = await setup_charger(hass)
    current = entity_id(hass, entry.entry_id, "charging_current", "number")
    await call(hass, "number", "set_value", {"entity_id": current, "value": 10})
    assert settings_of(hass, entry.entry_id).amps == 10

    # Somebody else writes the same settings (the app, another automation): the revision moves
    # on while this entity still displays the revision it last rendered.
    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(
        entry.entry_id, mutate=lambda settings: replace(settings, requested_kwh=31.0)
    )
    assert settings_of(hass, entry.entry_id).revision == 2

    with pytest.raises(ServiceValidationError) as refusal:
        await call(hass, "number", "set_value", {"entity_id": current, "value": 16})
    assert refusal.value.translation_key == "revision_conflict"

    settings = settings_of(hass, entry.entry_id)
    assert settings.amps == 10 and settings.requested_kwh == 31.0, "the newer edit survives"
    assert hass.states.get(current) is not None
    # The refusal re-rendered from the store, so the entity now shows what is actually stored
    # and the next write carries the current revision.
    await call(hass, "number", "set_value", {"entity_id": current, "value": 16})
    assert settings_of(hass, entry.entry_id).amps == 16


async def test_area_options_come_only_from_the_catalogue(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """The options are what the relay published, plus the configured area and nothing else."""
    entry = await setup_charger(hass)
    state = hass.states.get(entity_id(hass, entry.entry_id, "price_area", "select"))
    assert state is not None
    published = [area for area in state.attributes["options"] if area]
    assert published == ["DE-LU", "DK1", "NO1", "SE4"], "exactly the catalogue's ids"
    assert state.attributes["area_names"][SE4] == "SE4" or state.attributes["area_names"][SE4]


async def test_a_configured_area_survives_an_offline_catalogue(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Losing the relay never loses the choice: it stays selected and stays an option."""
    entry = await setup_charger(hass)
    area = entity_id(hass, entry.entry_id, "price_area", "select")
    await call(hass, "select", "select_option", {"entity_id": area, "option": "DK1"})

    # The relay stops answering for the catalogue: a refresh attempt fails, and the selector is
    # told the retained list is now stale -- not that it is fresh, and not silence.
    transport.serve("/v1/areas.json", 500, "")
    manager = domain_data(hass).price_refresh
    repository = domain_data(hass).price_repository
    assert manager is not None and repository is not None
    await repository.async_get_catalogue(refresh=True)
    await manager.async_ensure_catalogue()
    await hass.async_block_till_done()

    state = hass.states.get(area)
    assert state is not None and state.state == "DK1", "the configured area is untouched"
    assert "DK1" in state.attributes["options"]
    assert state.attributes["catalogue_state"] == "stale", "the retained list is not presented as fresh"
    assert settings_of(hass, entry.entry_id).area_id == "DK1"
    # And the entity still works: a market that is already configured stays selectable.
    await call(hass, "select", "select_option", {"entity_id": area, "option": "DK1"})

FISCAL = (
    # component, policy select key, value number key
    ("vat", "fiscal_vat_policy", "vat_rate"),
    ("tax", "fiscal_tax_policy", "energy_tax"),
    ("transfer", "fiscal_transfer_policy", "transfer_fee"),
)

#: What the fixture catalogue publishes, per market, for the three components. A `None` is a
#: market that publishes no suggestion for that component, which is a state of its own.
SUGGESTIONS = {
    SE4: {"vat": 25.0, "tax": 36.0, "transfer": 30.0},
    NO1: {"vat": 25.0, "tax": 7.13, "transfer": 0.0},
    DK1: {"vat": 25.0, "tax": 0.0, "transfer": None},
    DE_LU: {"vat": None, "tax": None, "transfer": None},
}


def override_of(hass: HomeAssistant, entry_id: str, component: str) -> Any:
    settings = settings_of(hass, entry_id)
    assert settings.area_id is not None
    return getattr(settings.override_for(settings.area_id), component)


@pytest.mark.parametrize(("component", "policy_key", "value_key"), FISCAL)
async def test_every_fiscal_component_round_trips_its_three_policies(
    hass: HomeAssistant,
    transport: StubTransport,
    component: str,
    policy_key: str,
    value_key: str,
) -> None:
    """Off, suggested and manual are three states, and each survives a round trip."""
    entry = await setup_charger(hass)
    await go_auto(hass)
    policy = entity_id(hass, entry.entry_id, policy_key, "select")
    value = entity_id(hass, entry.entry_id, value_key, "number")

    assert hass.states.get(policy).state == "off"
    assert hass.states.get(value).state == "unknown"
    assert override_of(hass, entry.entry_id, component).enabled is False

    # Suggested: on, with no override -- never stored as zero, and the figure the plan will
    # actually use is shown, with its source named.
    await call(hass, "select", "select_option", {"entity_id": policy, "option": "suggested"})
    override = override_of(hass, entry.entry_id, component)
    assert (override.enabled, override.value) == (True, None)
    assert hass.states.get(policy).state == "suggested"
    shown = hass.states.get(value)
    assert float(shown.state) == SUGGESTIONS[SE4][component], "the suggestion, not unknown"
    assert shown.attributes["value_source"] == "suggested"
    assert shown.attributes["explicit_value"] is None, "a suggestion is not an override"
    assert shown.attributes["suggested_value"] == SUGGESTIONS[SE4][component]
    assert shown.attributes["effective_value"] == SUGGESTIONS[SE4][component]
    assert shown.attributes["effective"] is True

    # Manual through the value entity: one write, and the policy follows it.
    await call(hass, "number", "set_value", {"entity_id": value, "value": 7.5})
    override = override_of(hass, entry.entry_id, component)
    assert (override.enabled, override.value) == (True, 7.5)
    assert hass.states.get(policy).state == "manual"
    assert float(hass.states.get(value).state) == 7.5

    # Off again: disabled, and the explicit value is *kept* for later -- shown, but named as
    # retained rather than charged.
    await call(hass, "select", "select_option", {"entity_id": policy, "option": "off"})
    override = override_of(hass, entry.entry_id, component)
    assert (override.enabled, override.value) == (False, 7.5)
    assert hass.states.get(policy).state == "off"
    retained = hass.states.get(value)
    assert float(retained.state) == 7.5, "the remembered figure does not disappear"
    assert retained.attributes["value_source"] == "off"
    assert retained.attributes["explicit_value"] == 7.5
    assert retained.attributes["effective_value"] is None and retained.attributes["effective"] is False

    # Manual again finds the value that was typed, and back to suggested clears it by design.
    await call(hass, "select", "select_option", {"entity_id": policy, "option": "manual"})
    assert override_of(hass, entry.entry_id, component).value == 7.5
    await call(hass, "select", "select_option", {"entity_id": policy, "option": "suggested"})
    assert override_of(hass, entry.entry_id, component).value is None


async def test_manual_without_a_value_is_refused_and_a_zero_is_a_real_value(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Manual means somebody typed a figure: none is refused, and zero is a figure."""
    entry = await setup_charger(hass)
    await go_auto(hass)
    policy = entity_id(hass, entry.entry_id, "fiscal_vat_policy", "select")
    value = entity_id(hass, entry.entry_id, "vat_rate", "number")

    with pytest.raises(ServiceValidationError) as refusal:
        await call(hass, "select", "select_option", {"entity_id": policy, "option": "manual"})
    assert refusal.value.translation_key == "invalid_fiscal"
    assert hass.states.get(policy).state == "off", "the refusal changed nothing"
    assert override_of(hass, entry.entry_id, "vat").value is None

    # An explicit zero is a stored figure, and it is not the same state as suggested.
    await call(hass, "number", "set_value", {"entity_id": value, "value": 0})
    override = override_of(hass, entry.entry_id, "vat")
    assert (override.enabled, override.value) == (True, 0.0)
    assert hass.states.get(policy).state == "manual"
    await call(hass, "select", "select_option", {"entity_id": policy, "option": "suggested"})
    override = override_of(hass, entry.entry_id, "vat")
    assert (override.enabled, override.value) == (True, None)
    assert hass.states.get(policy).state == "suggested"


async def test_numbers_refuse_rather_than_clamp(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Out of range and non-whole values are refused, and nothing is rounded into place."""
    entry = await setup_charger(hass)
    await go_auto(hass)
    amps = entity_id(hass, entry.entry_id, "charging_current", "number")
    periods = entity_id(hass, entry.entry_id, "maximum_periods", "number")
    energy = entity_id(hass, entry.entry_id, "requested_energy", "number")

    assert hass.states.get(amps).attributes["min"] == ABSOLUTE_MIN_AMPS
    assert hass.states.get(amps).attributes["max"] == ABSOLUTE_MAX_AMPS

    with pytest.raises(ServiceValidationError):
        await call(
            hass, "number", "set_value", {"entity_id": amps, "value": ABSOLUTE_MAX_AMPS + 75}
        )
    assert settings_of(hass, entry.entry_id).amps == 10, "nothing was clamped or stored"

    with pytest.raises(ServiceValidationError) as refusal:
        await call(hass, "number", "set_value", {"entity_id": amps, "value": 6.5})
    assert refusal.value.translation_key == "invalid_amps"
    assert settings_of(hass, entry.entry_id).amps == 10

    with pytest.raises(ServiceValidationError):
        await call(hass, "number", "set_value", {"entity_id": periods, "value": 9})
    assert settings_of(hass, entry.entry_id).max_periods == 1

    with pytest.raises(ServiceValidationError) as refusal:
        await call(hass, "number", "set_value", {"entity_id": periods, "value": 2.5})
    assert refusal.value.translation_key == "invalid_periods"
    assert settings_of(hass, entry.entry_id).max_periods == 1

    with pytest.raises(ServiceValidationError):
        await call(hass, "number", "set_value", {"entity_id": energy, "value": 0})
    assert settings_of(hass, entry.entry_id).requested_kwh == 20.0


async def test_every_number_reaches_the_plain_figures_a_person_types(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """The step grid starts at the minimum, so a coarse step silently skips the round numbers.

    Home Assistant's number card offers `min + k * step` and nothing else, so `min = 0.1` with
    `step = 0.5` offers `41.6` and `42.1` while refusing `42.0` -- a value the backend stores
    without complaint. Each number entity is checked for that shape, and the energy figure is
    pinned to the three values that exposed it.
    """
    entry = await setup_charger(hass)
    await go_auto(hass)

    keys = ("charging_current", "requested_energy", "maximum_periods")
    for key in keys:
        attributes = hass.states.get(entity_id(hass, entry.entry_id, key, "number")).attributes
        minimum, step, maximum = (attributes[bound] for bound in ("min", "step", "max"))
        assert step > 0, (key, attributes)
        # The first whole figure a person would type must be one the box can actually hold.
        plain = float(math.ceil(minimum))
        assert minimum <= plain <= maximum, (key, attributes)
        steps = (plain - minimum) / step
        assert abs(steps - round(steps)) < 1e-9, (key, attributes, plain)

    energy = entity_id(hass, entry.entry_id, "requested_energy", "number")
    for value in (41.6, 42.0, 42.1):
        await call(hass, "number", "set_value", {"entity_id": energy, "value": value})
        # Stored exactly, and read back exactly: no rounding, no float noise, no clamping.
        assert settings_of(hass, entry.entry_id).requested_kwh == value
        assert float(hass.states.get(energy).state) == value


async def test_the_departure_is_a_naive_wall_time_that_survives_a_reload(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """07:35 means 07:35 where the charger is, and it is still that after a reload."""
    entry = await setup_charger(hass)
    clock = entity_id(hass, entry.entry_id, "departure_time", "time")

    # `time.set_value`'s field is `time`, unlike `number.set_value`'s `value`.
    await call(hass, "time", "set_value", {"entity_id": clock, "time": time(7, 35)})

    stored = settings_of(hass, entry.entry_id).departure
    assert stored == time(7, 35) and stored.tzinfo is None, "naive, area-local, as stored"
    assert hass.states.get(clock).state == "07:35:00"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id(hass, entry.entry_id, "departure_time", "time")).state == (
        "07:35:00"
    )
    assert settings_of(hass, entry.entry_id).departure == time(7, 35)

async def test_switching_market_changes_units_without_copying_overrides(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """SE4's öre figures never become DE-LU's, and the units follow the market."""
    entry = await setup_charger(hass)
    await go_auto(hass)
    area = entity_id(hass, entry.entry_id, "price_area", "select")
    tax = entity_id(hass, entry.entry_id, "energy_tax", "number")
    policy = entity_id(hass, entry.entry_id, "fiscal_tax_policy", "select")

    se_unit = hass.states.get(tax).attributes["unit_of_measurement"]
    assert se_unit and se_unit.endswith("/kWh") and "öre" in se_unit
    await call(hass, "number", "set_value", {"entity_id": tax, "value": 45.0})
    assert hass.states.get(policy).state == "manual"

    await call(hass, "select", "select_option", {"entity_id": area, "option": DE_LU})

    de_unit = hass.states.get(tax).attributes["unit_of_measurement"]
    assert de_unit and de_unit != se_unit, "the money unit follows the market"
    assert hass.states.get(policy).state == "off", "DE-LU has its own, empty choice"
    assert hass.states.get(tax).state == "unknown", "and no copied figure"
    assert settings_of(hass, entry.entry_id).area_id == DE_LU
    assert settings_of(hass, entry.entry_id).override_for(SE4).tax.value == 45.0
    assert settings_of(hass, entry.entry_id).override_for(DE_LU).tax.value is None

    # Back to SE4: what was typed there is still there, and the unit is öre again.
    await call(hass, "select", "select_option", {"entity_id": area, "option": SE4})
    assert hass.states.get(tax).attributes["unit_of_measurement"] == se_unit
    assert float(hass.states.get(tax).state) == 45.0
    assert hass.states.get(policy).state == "manual"


async def test_both_chargers_update_from_one_catalogue_fetch_and_one_listener_each(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """A catalogue refresh is one request for the installation, and one listener per entity."""
    await setup_charger(hass, entry_id="entry_a", charge_control="switch.charger_a")
    await setup_charger(
        hass, entry_id="entry_b", charge_control="switch.charger_b", webhook_id="webhook-b"
    )
    manager = domain_data(hass).price_refresh
    assert manager is not None

    await go_auto(hass, "entry_a")
    await go_auto(hass, "entry_b", area_id=DE_LU)
    # One per area selector *and* one per loaded charger's market observation (the dashboard's own
    # display need), and no more: the observation is a second, legitimate consumer of the same list.
    assert len(manager._catalogue_listeners) == 4, "two selectors, two observations, and no more"

    catalogs_before = transport.call_count("/v1/areas.json")
    # The relay publishes a market list with one more market. One load, then one broadcast:
    # no area subscription is asked for, and none is needed.
    repository = domain_data(hass).price_repository
    assert repository is not None
    catalogue = json.loads((Path("tests/fixtures/relay/areas.json")).read_text())
    catalogue["areas"].append(dict(catalogue["areas"][0], id="NO2", name="Norway NO2"))
    transport.serve("/v1/areas.json", 200, json.dumps(catalogue))
    await repository.async_get_catalogue(refresh=True)
    await manager.async_ensure_catalogue()
    await hass.async_block_till_done()

    assert transport.call_count("/v1/areas.json") - catalogs_before <= 1, "one fetch, not one per charger"
    for entry_id_value in ("entry_a", "entry_b"):
        state = hass.states.get(entity_id(hass, entry_id_value, "price_area", "select"))
        assert state is not None and "NO2" in state.attributes["options"]
    assert len(manager._catalogue_listeners) == 4, "still exactly one listener per consumer"

@freeze_time("2026-09-22 06:00:00")
async def test_the_published_states_are_the_backends_and_every_state_is_translated(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Two reachable plan states plus the stale one are driven live, and all are translated.

    Deliberately not claiming more than that: the remaining planner states (`nothing_to_charge`,
    `planning_unavailable`, `error`, `proposal_unpriced`, `waiting_for_prices`) are
    pinned by the Auto controller and diagnostics suites, which drive them directly. What this
    test adds is the *entity-level* mapping -- the sensor's native state is the backend's own
    string, and every value the sensor can ever take has an English and a Swedish name.
    """
    entry = await setup_charger(hass)
    plan_state = entity_id(hass, entry.entry_id, "auto_plan_state")
    execution_state = entity_id(hass, entry.entry_id, "auto_execution_state")

    # Incomplete, then ready, then stale.
    assert hass.states.get(plan_state).state == "incomplete_settings"

    await go_auto(hass)
    assert hass.states.get(plan_state).state in ("proposal_ready", "proposal_unpriced")
    snapshot_state = preview_for(hass, entry.entry_id).snapshot().state

    # The relay stops listing today: the manager's own refresh path sees that, and the next
    # calculation is stale rather than freshly served.
    serve_index(transport, {"SE4": []})
    manager = domain_data(hass).price_refresh
    assert manager is not None
    await manager.async_refresh_area(SE4)
    await preview_for(hass, entry.entry_id).async_recalculate()
    await hass.async_block_till_done()
    assert hass.states.get(plan_state).state == "price_data_stale"

    # Every state and reason this release can report has an English and a Swedish name, so a
    # dashboard never shows a machine code.
    for language in ("en", "sv"):
        document = json.loads(
            (Path("custom_components/spotnav/translations") / f"{language}.json").read_text()
        )
        states = document["entity"]["sensor"]["auto_plan_state"]["state"]
        assert set(states) == {
            "incomplete_settings",
            "waiting_for_prices",
            "waiting_for_publication",
            "price_data_stale",
            "proposal_ready",
            "proposal_unpriced",
            "nothing_to_charge",
            "planning_unavailable",
            "error",
        }
        executions = document["entity"]["sensor"]["auto_execution_state"]["state"]
        assert set(executions) == {
            "not_applied",
            "scheduled",
            "active",
            "paused",
            "complete",
            "apply_pending",
            "execution_error",
        }
    assert snapshot_state in ("proposal_ready", "proposal_unpriced")


# The fixture day is 2026-09-22, so the clock is pinned to its morning: with flat prices the
# plan's window is then the hour we are in, which is what makes a change wait for a boundary
# instead of replacing the plan at once.
#
# The same instant is pinned on the four tests further down that *install* an Auto plan through
# the entities (`test_choosing_auto_explicitly_enables_it_and_installs_once`,
# `test_the_published_states_are_the_backends_and_every_state_is_translated`,
# `test_the_three_auto_buttons_reach_the_backend_exactly_once` and
# `test_a_partial_pause_reads_as_an_execution_error_not_a_clean_pause`). They all end with a plan
# whose window is relative to this fixture day, and `ChargingController` validates a fresh plan
# against `dt_util.utcnow()` -- the *real* clock -- so outside a freeze their answer depended on
# what time of day the suite was run: green in the morning, `controller.plan is None` in the
# afternoon. Frozen, every one of them answers the same at any hour. Nothing else here freezes.
@freeze_time("2026-09-22 06:00:00")
async def test_a_waiting_change_is_visibly_not_the_installed_plan(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Pending, applied and historical are three different readings, not one.

    Flat prices put the plan's window at the fixture clock, so the charge is under way and a
    material change has to wait for the boundary rather than replacing it.
    """
    entry = await setup_charger(hass)
    serve(transport, flat=True)
    await go_auto(hass)
    plan_state = entity_id(hass, entry.entry_id, "auto_plan_state")
    execution_state = entity_id(hass, entry.entry_id, "auto_execution_state")
    cost = entity_id(hass, entry.entry_id, "auto_cost")
    periods = entity_id(hass, entry.entry_id, "auto_periods")

    applied = hass.states.get(plan_state)
    assert applied.attributes["applied"] is True and applied.attributes["historical"] is False
    assert hass.states.get(execution_state).attributes["applied_identity"] is not None
    assert hass.states.get(periods).attributes["periods"], "bounded start/end pairs"
    assert len(hass.states.get(periods).attributes["periods"]) <= 8
    assert hass.states.get(cost).attributes["unit_of_measurement"] == "SEK"

    preview = preview_for(hass, entry.entry_id)
    assert preview is not None
    before = preview.snapshot()
    assert before.execution in ("active", "scheduled")

    # A material change while the window is charging: it waits for the boundary, and the
    # entities say so rather than presenting it as scheduled.
    await preview.async_apply_settings(
        mutate=lambda settings: replace(settings, requested_kwh=settings.requested_kwh + 12)
    )
    await hass.async_block_till_done()

    waiting = hass.states.get(plan_state)
    execution = hass.states.get(execution_state)
    assert execution.state == "apply_pending"
    assert waiting.attributes["applied"] is False, "not scheduled"
    assert waiting.attributes["historical"] is False
    assert execution.attributes["pending_identity"] is not None
    assert execution.attributes["pending_attempt"] is not None
    assert execution.attributes["applied_identity"] != execution.attributes["pending_identity"]


async def test_no_auto_sensor_exposes_an_array_an_identifier_or_an_exception(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Attributes carry compact facts: no price arrays, no ids, no exception text."""
    entry = await setup_charger(hass)
    await go_auto(hass)
    forbidden_keys = ("webhook", "owner", "vehicle", "entity_id", "interval", "document", "prices", "entry_id")
    forbidden_values = ("Traceback", "Exception", "webhook_", "switch.")

    registry = er.async_get(hass)
    checked = 0
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if "auto_" not in entity.unique_id and not entity.unique_id.endswith(
            ("_planning_mode", "_price_area", "_charging_phases", "_fiscal_vat_policy")
        ):
            continue
        state = hass.states.get(entity.entity_id)
        assert state is not None
        checked += 1
        for key, value in state.attributes.items():
            assert not any(bad in key for bad in forbidden_keys), key
            text = json.dumps(value, default=str)
            assert not any(bad in text for bad in forbidden_values), (key, text)
            if isinstance(value, list):
                assert len(value) <= 8, key
    assert checked >= 10

# --------------------------------------------------------- actions, lifecycle, the example


class Counted:
    """A method of the reviewed backend, counted, with the original still called."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, owner: Any, name: str) -> None:
        self.calls = 0
        original = getattr(owner, name)

        async def spy(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, name, spy)


@freeze_time("2026-09-22 06:00:00")
async def test_the_three_auto_buttons_reach_the_backend_exactly_once(
    hass: HomeAssistant, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every button is one reviewed call, and the state afterwards is the review's state."""
    entry = await setup_charger(hass)
    await go_auto(hass)
    preview = preview_for(hass, entry.entry_id)
    assert preview is not None
    recalculate = Counted(monkeypatch, preview, "async_recalculate")
    pause = Counted(monkeypatch, preview, "async_pause")
    resume = Counted(monkeypatch, preview, "async_resume")

    recalculate_button = entity_id(hass, entry.entry_id, "recalculate_auto", "button")
    pause_button = entity_id(hass, entry.entry_id, "pause_auto", "button")
    resume_button = entity_id(hass, entry.entry_id, "resume_auto", "button")
    execution = entity_id(hass, entry.entry_id, "auto_execution_state")

    await call(hass, "button", "press", {"entity_id": recalculate_button})
    assert recalculate.calls == 1 and pause.calls == 0 and resume.calls == 0

    await call(hass, "button", "press", {"entity_id": pause_button})
    assert pause.calls == 1
    assert hass.states.get(execution).state in ("paused", "execution_error")
    assert settings_of(hass, entry.entry_id).execution_paused is True

    await call(hass, "button", "press", {"entity_id": resume_button})
    assert resume.calls == 1
    assert settings_of(hass, entry.entry_id).execution_paused is False
    assert hass.states.get(execution).state in ("scheduled", "active", "complete")

    cancel = Counted(monkeypatch, controller_of(hass, entry.entry_id), "async_cancel")
    await call(
        hass,
        "button",
        "press",
        {"entity_id": entity_id(hass, entry.entry_id, "cancel", "button")},
    )
    assert cancel.calls == 1
    assert controller_of(hass, entry.entry_id).plan is None


async def test_recalculate_incomplete_settings_is_safe(
    hass: HomeAssistant, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An external charger keeps its authority: the button refuses rather than flipping it."""
    entry = await setup_charger(hass)
    preview = preview_for(hass, entry.entry_id)
    assert preview is not None
    counted = Counted(monkeypatch, preview, "async_recalculate")
    button = entity_id(hass, entry.entry_id, "recalculate_auto", "button")

    assert hass.states.get(button).state == "unknown"
    await call(hass, "button", "press", {"entity_id": button})
    assert counted.calls == 1
    assert preview.snapshot().state == "incomplete_settings"


@freeze_time("2026-09-22 06:00:00")
async def test_a_partial_pause_reads_as_an_execution_error_not_a_clean_pause(
    hass: HomeAssistant, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the stop fails, the pause stands but is reported as a failure."""
    entry = await setup_charger(hass)
    await go_auto(hass)
    controller = controller_of(hass, entry.entry_id)
    assert controller.plan is not None

    async def refuse(*, clear_schedule: bool = False) -> None:
        raise RuntimeError("the charger said no")

    monkeypatch.setattr(controller, "async_stop", refuse)
    await call(
        hass,
        "button",
        "press",
        {"entity_id": entity_id(hass, entry.entry_id, "pause_auto", "button")},
    )

    execution = hass.states.get(entity_id(hass, entry.entry_id, "auto_execution_state"))
    assert execution.state == "execution_error"
    assert execution.attributes["execution_error"] == "pause_stop_failed"
    assert execution.attributes["paused"] is True
    assert "charger said no" not in json.dumps(execution.attributes)


async def test_unloading_removes_the_listeners_and_late_callbacks_are_inert(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """An unloaded charger leaves no listener behind, and a refresh afterwards is harmless."""
    entry = await setup_charger(hass)
    await go_auto(hass)
    manager = domain_data(hass).price_refresh
    assert manager is not None
    # The area selector's listener and the market observation's -- the observability of the graph the
    # *other* tests in this module are about.
    assert len(manager._catalogue_listeners) == 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert manager._catalogue_listeners == [], "both listeners went with the entry"
    assert preview_for(hass, entry.entry_id) is None
    assert executor_for(hass, entry.entry_id) is None

    # A catalogue change after the unload reaches nobody, and nothing raises.
    transport.serve("/v1/areas.json", 500, "")
    await manager.async_ensure_catalogue()
    await hass.async_block_till_done()


async def test_a_reload_creates_no_duplicate_entities_or_listeners(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """The platform setup is idempotent per entry: same entities, one listener each."""
    entry = await setup_charger(hass)
    await go_auto(hass)
    manager = domain_data(hass).price_refresh
    assert manager is not None
    before = registered(hass, entry.entry_id)
    assert len(manager._catalogue_listeners) == 2

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert registered(hass, entry.entry_id) == before
    assert len(manager._catalogue_listeners) == 2, "still one per consumer, not two each"
    entries = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    auto_ids = [item.unique_id for item in entries if "auto" in item.unique_id or "fiscal" in item.unique_id]
    assert auto_ids and len(auto_ids) == len(set(auto_ids)), "no Auto entity registered twice"
    assert len(entries) == 36, "the surface this release creates, counted once"








async def test_a_fiscal_figure_names_its_source_and_the_effective_value(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Four sources, four readings: the number shown is the number the plan would use."""
    entry = await setup_charger(hass)
    await go_auto(hass)
    value = entity_id(hass, entry.entry_id, "vat_rate", "number")
    policy = entity_id(hass, entry.entry_id, "fiscal_vat_policy", "select")

    # off, with nothing stored: nothing to show, nothing charged, and it says so.
    off = hass.states.get(value)
    assert off.state == "unknown"
    assert (off.attributes["value_source"], off.attributes["effective"]) == ("off", False)
    assert off.attributes["effective_value"] is None

    # manual: what somebody typed.
    await call(hass, "number", "set_value", {"entity_id": value, "value": 8.5})
    manual = hass.states.get(value)
    assert (manual.state, manual.attributes["value_source"]) == ("8.5", "manual")
    assert (manual.attributes["explicit_value"], manual.attributes["effective_value"]) == (8.5, 8.5)
    assert manual.attributes["suggested_value"] == SUGGESTIONS[SE4]["vat"]
    assert manual.attributes["effective"] is True

    # suggested: the catalogue's own figure, with no override stored.
    await call(hass, "select", "select_option", {"entity_id": policy, "option": "suggested"})
    suggested = hass.states.get(value)
    assert suggested.attributes["value_source"] == "suggested"
    assert float(suggested.state) == SUGGESTIONS[SE4]["vat"]
    assert suggested.attributes["explicit_value"] is None
    assert suggested.attributes["effective_value"] == SUGGESTIONS[SE4]["vat"]

    # suggestion_unavailable: DK1 publishes no grid fee, so that figure cannot be resolved.
    transfer_policy = entity_id(hass, entry.entry_id, "fiscal_transfer_policy", "select")
    transfer = entity_id(hass, entry.entry_id, "transfer_fee", "number")
    area = entity_id(hass, entry.entry_id, "price_area", "select")
    await call(hass, "select", "select_option", {"entity_id": area, "option": DK1})
    await call(hass, "select", "select_option", {"entity_id": transfer_policy, "option": "suggested"})
    unresolved = hass.states.get(transfer)
    assert unresolved.attributes["value_source"] == "suggestion_unavailable"
    assert unresolved.state == "unknown"
    assert unresolved.attributes["effective_value"] is None
    assert unresolved.attributes["effective"] is False
    # And it is still a suggestion policy, not silently a manual zero.
    assert hass.states.get(transfer_policy).state == "suggested"
    assert override_of(hass, entry.entry_id, "transfer").value is None


async def test_a_literal_zero_is_a_real_value_in_both_policies(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """NO1's published zero fee is a figure, and a typed zero is a figure too."""
    entry = await setup_charger(hass)
    await go_auto(hass)
    area = entity_id(hass, entry.entry_id, "price_area", "select")
    policy = entity_id(hass, entry.entry_id, "fiscal_transfer_policy", "select")
    value = entity_id(hass, entry.entry_id, "transfer_fee", "number")

    await call(hass, "select", "select_option", {"entity_id": area, "option": NO1})
    await call(hass, "select", "select_option", {"entity_id": policy, "option": "suggested"})
    shown = hass.states.get(value)
    assert shown.attributes["suggested_value"] == 0.0
    assert float(shown.state) == 0.0, "a published zero is shown, not blanked"
    assert shown.attributes["value_source"] == "suggested"
    assert shown.attributes["explicit_value"] is None

    await call(hass, "number", "set_value", {"entity_id": value, "value": 0})
    typed = hass.states.get(value)
    assert float(typed.state) == 0.0 and typed.attributes["value_source"] == "manual"
    assert typed.attributes["explicit_value"] == 0.0
    assert override_of(hass, entry.entry_id, "transfer").value == 0.0


async def test_reading_and_rendering_a_fiscal_figure_writes_nothing(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Displaying a suggestion must not turn it into a stored value."""
    entry = await setup_charger(hass)
    await go_auto(hass)
    store = domain_data(hass).auto_store
    assert store is not None
    revision = settings_of(hass, entry.entry_id).revision

    await call(
        hass,
        "select",
        "select_option",
        {
            "entity_id": entity_id(hass, entry.entry_id, "fiscal_vat_policy", "select"),
            "option": "suggested",
        },
    )
    after_policy = settings_of(hass, entry.entry_id).revision
    stored = settings_of(hass, entry.entry_id).override_for(SE4)
    assert stored.vat.value is None and stored.vat.enabled is True

    # Reading everything the surface exposes, twice, changes nothing at all.
    for _ in range(2):
        for suffix, platform in (
            ("vat_rate", "number"),
            ("energy_tax", "number"),
            ("transfer_fee", "number"),
            ("fiscal_vat_policy", "select"),
            ("price_area", "select"),
        ):
            state = hass.states.get(entity_id(hass, entry.entry_id, suffix, platform))
            assert state is not None
            assert state.attributes.get("suggested_value", "absent") != "written"
        await hass.async_block_till_done()

    assert settings_of(hass, entry.entry_id).revision == after_policy
    assert after_policy == revision + 1, "exactly the one write the policy change made"
    assert settings_of(hass, entry.entry_id).override_for(SE4).vat.value is None

# ---------------------------------------------- catalogue notification (one shared broadcast)


def catalogue_deliveries(count: list) -> int:
    """How many of these deliveries carried a catalogue with markets in it."""
    return sum(1 for snapshot in count if snapshot.catalogue is not None)


async def test_an_external_charger_gets_the_market_list_without_an_area_subscription(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Loading the catalogue must reach the selector even with nothing subscribed to a price.

    A charger in `external` mode subscribes to no area, so the manager's own cycle never runs
    for it: the market list can only reach its selector through the ensure path's notification.
    """
    transport.serve("/v1/areas.json", 500, "")  # nothing can be loaded at setup
    entry = await setup_charger(hass)
    area = entity_id(hass, entry.entry_id, "price_area", "select")
    assert hass.states.get(area).attributes["options"] == [], "no catalogue, no options"
    assert controller_of(hass, entry.entry_id).plan is None, "and this charger is external"

    transport.serve_area(SE4)
    manager = domain_data(hass).price_refresh
    assert manager is not None
    await manager.async_ensure_catalogue()
    await hass.async_block_till_done()

    state = hass.states.get(area)
    assert [option for option in state.attributes["options"]] == ["DE-LU", "DK1", "NO1", "SE4"]
    assert state.attributes["catalogue_state"] == "ready"
    assert state.attributes["area_names"]["SE4"]
    assert controller_of(hass, entry.entry_id).plan is None, "still external, still nothing planned"


async def test_a_failed_catalogue_load_reaches_the_selector_with_its_configured_option(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """A refusal is reported as a state, and never costs the configured market."""
    entry = await setup_charger(hass)
    area = entity_id(hass, entry.entry_id, "price_area", "select")
    await call(hass, "select", "select_option", {"entity_id": area, "option": DK1})

    # The catalogue was loaded at setup, so a refresh that now fails leaves a *stale* list:
    # retained (it is still the best answer anyone has) and reported as not fresh.
    transport.serve("/v1/areas.json", 500, "")
    repository = domain_data(hass).price_repository
    manager = domain_data(hass).price_refresh
    assert repository is not None and manager is not None
    await repository.async_get_catalogue(refresh=True)
    assert repository.catalogue_snapshot().state == "stale"
    await manager.async_ensure_catalogue()
    await hass.async_block_till_done()

    state = hass.states.get(area)
    assert state.attributes["catalogue_state"] == "stale", "the failure is visible"
    assert state.state == "DK1" and "DK1" in state.attributes["options"]


async def test_a_listener_registered_after_a_change_still_hears_it(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Registering a listener must not acknowledge a change on an older listener's behalf."""
    await setup_charger(hass)
    repository = domain_data(hass).price_repository
    manager = domain_data(hass).price_refresh
    assert repository is not None and manager is not None

    first: list[Any] = []
    cancel_first = manager.add_catalogue_listener(first.append)
    assert first, "a new listener hears the current snapshot at once"
    before = catalogue_deliveries(first)

    # The repository changes without any broadcast: exactly what a load from storage does.
    catalogue = json.loads(Path("tests/fixtures/relay/areas.json").read_text())
    catalogue["areas"].append(dict(catalogue["areas"][0], id="NO2", name="Norway NO2"))
    transport.serve("/v1/areas.json", 200, json.dumps(catalogue))
    await repository.async_get_catalogue(refresh=True)
    assert catalogue_deliveries(first) == before, "a repository load is not a broadcast"

    second: list[Any] = []
    cancel_second = manager.add_catalogue_listener(second.append)
    assert second and second[0].catalogue is not None, "the newcomer hears it immediately"
    assert "NO2" in second[0].catalogue.area_ids
    assert catalogue_deliveries(first) == before, "still nothing on the older listener's back"

    # The broadcast: the older listener hears the change exactly once, and the newcomer once
    # more (the same snapshot it just received, which is what "immediately" costs).
    await manager.async_ensure_catalogue()
    assert catalogue_deliveries(first) == before + 1, "exactly once, and not on the newcomer's behalf"
    assert "NO2" in first[-1].catalogue.area_ids
    assert catalogue_deliveries(second) >= 1
    assert "NO2" in second[-1].catalogue.area_ids

    cancel_first()
    cancel_second()


async def test_concurrent_ensures_are_one_fetch_and_one_update_each(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Two chargers asking at the same moment share the fetch and the broadcast."""
    await setup_charger(hass, entry_id="entry_a", charge_control="switch.charger_a")
    await setup_charger(
        hass, entry_id="entry_b", charge_control="switch.charger_b", webhook_id="webhook-b"
    )
    manager = domain_data(hass).price_refresh
    assert manager is not None
    first: list[Any] = []
    second: list[Any] = []
    manager.add_catalogue_listener(first.append)
    manager.add_catalogue_listener(second.append)

    before = transport.call_count("/v1/areas.json")
    await asyncio.gather(
        manager.async_ensure_catalogue(), manager.async_ensure_catalogue(), return_exceptions=True
    )
    after = transport.call_count("/v1/areas.json")

    assert after - before <= 1, "one fetch for the installation, not one per charger"
    for deliveries in (first, second):
        # One broadcast each, on top of the initial delivery a new listener always gets.
        assert len(deliveries) - catalogue_deliveries(deliveries[:-1]) == 1

    # An identical revalidation is silent for both.
    counts = (len(first), len(second))
    await manager.async_ensure_catalogue()
    assert (len(first), len(second)) == counts, "an unchanged catalogue says nothing"
    for state_entry in ("entry_a", "entry_b"):
        options = hass.states.get(
            entity_id(hass, state_entry, "price_area", "select")
        ).attributes["options"]
        assert "SE4" in options


async def test_a_removed_listener_hears_nothing_and_a_late_load_is_inert(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Removal is idempotent, and a load that completes afterwards reaches nobody."""
    await setup_charger(hass)
    manager = domain_data(hass).price_refresh
    repository = domain_data(hass).price_repository
    assert manager is not None and repository is not None
    deliveries: list[Any] = []
    cancel = manager.add_catalogue_listener(deliveries.append)
    seen = len(deliveries)
    cancel()
    cancel()

    transport.hold("/v1/areas.json")
    catalogue = json.loads(Path("tests/fixtures/relay/areas.json").read_text())
    catalogue["areas"].append(dict(catalogue["areas"][0], id="NO3", name="Norway NO3"))
    transport.serve("/v1/areas.json", 200, json.dumps(catalogue))
    pending = asyncio.ensure_future(repository.async_get_catalogue(refresh=True))
    await asyncio.wait_for(transport.entered["/v1/areas.json"].wait(), timeout=5)
    transport.release("/v1/areas.json")
    await asyncio.wait_for(pending, timeout=5)
    await manager.async_ensure_catalogue()
    await hass.async_block_till_done()

    assert len(deliveries) == seen, "nothing reached the removed listener"

async def test_the_departure_time_service_keeps_whole_minutes_and_refuses_the_rest(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """A wall time is stored to the minute: an offset, seconds or microseconds are refused."""
    entry = await setup_charger(hass)
    clock = entity_id(hass, entry.entry_id, "departure_time", "time")

    await call(hass, "time", "set_value", {"entity_id": clock, "time": time(7, 35)})
    assert settings_of(hass, entry.entry_id).departure == time(7, 35)
    assert hass.states.get(clock).state == "07:35:00"

    refused = {
        "an offset": time(7, 35, tzinfo=timezone.utc),
        "seconds": time(7, 35, 30),
        "microseconds": time(7, 35, 0, 1),
    }
    for what, value in refused.items():
        with pytest.raises(ServiceValidationError) as refusal:
            await call(hass, "time", "set_value", {"entity_id": clock, "time": value})
        assert refusal.value.translation_key == "invalid_departure", what
        # Nothing was rounded into place, and nothing was stored.
        assert settings_of(hass, entry.entry_id).departure == time(7, 35), what
        assert hass.states.get(clock).state == "07:35:00", what
