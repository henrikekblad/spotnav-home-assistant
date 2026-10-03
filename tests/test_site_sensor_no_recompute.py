"""The site's sensors read what the evaluation already computed: writing their state recomputes nothing.

Home Assistant warned that updating `sensor.site_proposed_current_<charger>` and `sensor.site_capacity_state`
took about half a second: each write rebuilt the capability snapshot (every measurement and charger read),
the charger's measured current and the grid power. They are taken once per evaluation (`SiteEvaluation`).
"""

from __future__ import annotations

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
            monkeypatch.setattr(
                type(controller), name, property(lambda self, n=name, o=getattr(type(controller), name): _count(calls, n, o.fget(self)))
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
