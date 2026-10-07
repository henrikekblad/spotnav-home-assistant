"""The site's sensors read what the evaluation already computed: writing their state recomputes nothing.

Home Assistant warned that updating `sensor.site_proposed_current_<charger>` and `sensor.site_capacity_state`
took about half a second: each write rebuilt the capability snapshot (every measurement and charger read),
the charger's measured current and the grid power. They are taken once per evaluation (`SiteEvaluation`).
"""

from __future__ import annotations

import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController

from .world import controller_of, setup_site_with_charger

HEAVY = (
    "capability_snapshot",
    "charger_measured_current",
    "grid_power_snapshot",
    "_build_config",
    "_build_requests",
)


async def test_writing_the_site_sensors_does_no_recomputation(hass: HomeAssistant, monkeypatch: Any) -> None:
    charger, site = (await setup_site_with_charger(hass))[:2]
    controller = controller_of(hass, site.entry_id)
    assert isinstance(controller, SiteCapacityController)
    registry = er.async_get(hass)
    state_id = registry.async_get_entity_id("sensor", DOMAIN, f"{site.entry_id}_site_state")
    proposed_id = registry.async_get_entity_id("sensor", DOMAIN, f"{site.entry_id}_proposed_{charger.entry_id}")
    assert state_id is not None and proposed_id is not None
    before = {entity_id: dict(hass.states.get(entity_id).attributes) for entity_id in (state_id, proposed_id)}

    calls: dict[str, int] = {name: 0 for name in HEAVY}
    for name in HEAVY:
        original = getattr(controller, name)
        if isinstance(getattr(type(controller), name, None), property):
            original_property = getattr(type(controller), name)
            monkeypatch.setattr(
                type(controller),
                name,
                property(lambda self, n=name, o=original_property: _count(calls, n, o.fget(self))),
            )
        else:
            monkeypatch.setattr(
                controller, name, lambda *a, n=name, o=original, **k: _count(calls, n, o(*a, **k))
            )

    for _ in range(5):
        controller._notify()
    await hass.async_block_till_done()

    assert calls == {name: 0 for name in HEAVY}
    # The attributes are what they were: only the evaluation's own timing may differ.
    for entity_id, attributes in before.items():
        assert set(hass.states.get(entity_id).attributes) == set(attributes)


def _count(calls: dict[str, int], name: str, value: Any) -> Any:
    calls[name] += 1
    return value


def _site_sensors(hass: HomeAssistant, site_entry_id: str, charger_entry_id: str) -> list[Any]:
    wanted = {f"{site_entry_id}_site_state", f"{site_entry_id}_proposed_{charger_entry_id}"}
    entities = [entity for entity in hass.data["entity_components"]["sensor"].entities if entity.unique_id in wanted]
    assert len(entities) == 2
    return entities


SNAPSHOTS = (
    "yield_stepping_snapshot",
    "battery_probe_snapshot",
    "solar_surplus_snapshot",
    "hybrid_snapshot",
    "membership_conflicts",
)


async def test_reading_the_site_sensors_state_builds_nothing(hass: HomeAssistant, monkeypatch: Any) -> None:
    """The state and attributes are built when the site changes, so Home Assistant's timed read is a lookup.

    The warning came back after the snapshots above were moved into the evaluation: what was left was the
    attributes being assembled inside that read. They are built in the site's change callback instead.
    """
    charger, site = (await setup_site_with_charger(hass))[:2]
    controller = controller_of(hass, site.entry_id)
    entities = _site_sensors(hass, site.entry_id, charger.entry_id)
    calls = {name: 0 for name in SNAPSHOTS}
    for name in SNAPSHOTS:
        original = getattr(type(controller), name)
        monkeypatch.setattr(
            type(controller), name, property(lambda self, n=name, o=original: _count(calls, n, o.fget(self)))
        )
    monkeypatch.setattr(
        controller, "balancing_resume_snapshot", lambda o=controller.balancing_resume_snapshot: _count(calls, "resume", o())
    )
    calls["resume"] = 0

    for entity in entities:
        first = entity.extra_state_attributes
        for _ in range(50):
            entity._async_calculate_state()
        assert entity.extra_state_attributes is first

    assert set(calls.values()) == {0}

    # A change of the site builds them again.
    controller._notify()
    await hass.async_block_till_done()
    assert calls["solar_surplus_snapshot"] >= 1


async def test_a_site_sensor_state_write_is_cheap(hass: HomeAssistant) -> None:
    """A generous bound (Home Assistant warns at 0.4 s): 200 timed reads of both sensors in well under a second."""
    charger, site = (await setup_site_with_charger(hass))[:2]
    entities = _site_sensors(hass, site.entry_id, charger.entry_id)
    started = time.perf_counter()
    for _ in range(200):
        for entity in entities:
            entity._async_calculate_state()
    assert (time.perf_counter() - started) / 400 < 0.002
