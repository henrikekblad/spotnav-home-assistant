"""The site's built-on-change sensors are written only when what they show changes.

On a live site `sensor.site_capacity_state` was written with every meter reading (a quarter of all state
changes in that Home Assistant), and its attributes carried ages and raw readings that differ each time, so
every write was a new attributes row in the recorder: gigabytes in days. The attributes now carry only what
moves when the site's situation does, and a reading that changes nothing they show writes nothing.
"""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.sensor import ChargerProposedCurrentEntity, SiteStateEntity

from .helpers import set_current_sensor
from .world import controller_of, setup_site_with_charger

SITE = "site_writes"
CHARGER = "charger_writes"


async def _setup(hass: HomeAssistant) -> tuple[str, str]:
    for phase in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.{SITE}_{phase}", 10)
    await setup_site_with_charger(hass, charger_entry_id=CHARGER, site_entry_id=SITE)
    registry = er.async_get(hass)
    state_id = registry.async_get_entity_id("sensor", DOMAIN, f"{SITE}_site_state")
    proposed_id = registry.async_get_entity_id("sensor", DOMAIN, f"{SITE}_proposed_{CHARGER}")
    assert state_id is not None and proposed_id is not None
    return state_id, proposed_id


def _record_changes(hass: HomeAssistant, entity_ids: tuple[str, ...]) -> list[str]:
    changes: list[str] = []

    @callback
    def note(event: Event[Any]) -> None:
        if event.data["entity_id"] in entity_ids:
            changes.append(event.data["entity_id"])

    hass.bus.async_listen(EVENT_STATE_CHANGED, note)
    return changes


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


async def test_the_attributes_carry_no_age_and_no_raw_reading(hass: HomeAssistant) -> None:
    state_id, proposed_id = await _setup(hass)
    for entity_id in (state_id, proposed_id):
        keys = _keys(dict(hass.states.get(entity_id).attributes))
        assert not [key for key in keys if "age_s" in key or key.endswith("_at")], entity_id
    site_keys = set(hass.states.get(state_id).attributes)
    assert not site_keys & {
        "measured_site_current_a",
        "limiting_phase",
        "phase_headroom_a",
        "measured_margin_a",
        "phase_signed_active_power_w",
        "grid_power",
        "solar_surplus",
        "hybrid",
        "yield_stepping",
        "battery_probe",
        "balancing_resume",
    }
    assert not {"measured_charger_current_a", "limiting_phase"} & set(hass.states.get(proposed_id).attributes)


def test_settings_and_fixed_facts_stay_out_of_the_recorder() -> None:
    assert {"next_step", "membership_conflicts", "solar_priority"} <= SiteStateEntity._unrecorded_attributes
    assert {"reason", "site_activity", "confidence"}.isdisjoint(SiteStateEntity._unrecorded_attributes)
    assert "charger_entry_id" in ChargerProposedCurrentEntity._unrecorded_attributes
    assert {"state", "reason"}.isdisjoint(ChargerProposedCurrentEntity._unrecorded_attributes)


async def test_a_reading_that_only_moves_ages_and_jitter_writes_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    state_id, proposed_id = await _setup(hass)
    controller = controller_of(hass, SITE)
    changes = _record_changes(hass, (state_id, proposed_id))

    for step, amps in enumerate((10.04, 9.97, 10.2, 10.01, 9.8)):
        freezer.tick(2)
        before = dict(controller.result.measured_phase_current_a)
        for phase in ("l1", "l2", "l3"):
            set_current_sensor(hass, f"sensor.{SITE}_{phase}", amps)
        await hass.async_block_till_done()
        # The site did take the reading ...
        assert controller.result.measured_phase_current_a != before, step
    # ... and neither sensor was written for it.
    assert changes == []
    assert hass.states.get(state_id).state == "observing"


async def test_a_change_of_state_or_reason_is_written(hass: HomeAssistant) -> None:
    state_id, proposed_id = await _setup(hass)
    changes = _record_changes(hass, (state_id, proposed_id))
    reason = hass.states.get(state_id).attributes["reason"]

    hass.states.async_set(f"sensor.{SITE}_l2", "unavailable")
    await hass.async_block_till_done()

    state = hass.states.get(state_id)
    assert state.state != "observing"
    assert state.attributes["reason"] != reason
    assert state_id in changes

    changes.clear()
    set_current_sensor(hass, f"sensor.{SITE}_l2", 10)
    await hass.async_block_till_done()
    assert hass.states.get(state_id).state == "observing"
    assert state_id in changes


async def test_a_write_home_assistant_makes_itself_shows_what_was_built(hass: HomeAssistant) -> None:
    state_id, _proposed_id = await _setup(hass)
    entity = next(
        entity
        for entity in hass.data["entity_components"]["sensor"].entities
        if entity.entity_id == state_id
    )
    before = hass.states.get(state_id)
    entity.async_write_ha_state()
    await hass.async_block_till_done()
    after = hass.states.get(state_id)
    assert after.state == before.state
    assert dict(after.attributes) == dict(before.attributes)
