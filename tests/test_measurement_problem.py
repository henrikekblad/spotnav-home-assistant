"""Which phases make a site's measurement unusable, and why: named from the site result, with the
entity each phase is read from, in the site controller and in the card's `get_entity_config`."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.entity_fields import site_measurement_info

from .helpers import make_site_entry, set_current_sensor
from .world import controller_of


async def _site(hass: HomeAssistant, entry_id: str):
    entry = make_site_entry(hass, entry_id=entry_id, main_fuse_a=25.0, charger_entry_ids=[])
    for phase in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.{entry_id}_{phase}", 4)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_a_healthy_measurement_has_no_problem(hass: HomeAssistant) -> None:
    entry = await _site(hass, "site_ok")
    assert controller_of(hass, entry.entry_id).measurement_problem is None
    assert [w["code"] for w in site_measurement_info(hass, entry)["warnings"]] == []


async def test_phases_without_a_value_and_stale_phases_are_named_with_their_entities(
    hass: HomeAssistant, freezer
) -> None:
    entry = await _site(hass, "site_bad")
    controller = controller_of(hass, entry.entry_id)
    freezer.tick(400)
    # L1 is read again; L2 vanished; L3 is left as it was and is now older than the maximum age.
    set_current_sensor(hass, "sensor.site_bad_l1", 5)
    hass.states.async_remove("sensor.site_bad_l2")
    controller._recompute()

    problem = controller.measurement_problem
    assert problem is not None
    assert [(item.phase, item.cause, item.entity_id) for item in problem.phases] == [
        ("L2", "no_value", "sensor.site_bad_l2"),
        ("L3", "stale", "sensor.site_bad_l3"),
    ]
    assert problem.max_age_s == 120.0

    [warning, *_] = site_measurement_info(hass, entry)["warnings"]
    assert warning["code"] == "measurement_unhealthy"
    assert warning["interval_s"] == 120.0
    assert [(p["phase"], p["cause"], p["entity_id"]) for p in warning["phases"]] == [
        ("L2", "no_value", "sensor.site_bad_l2"),
        ("L3", "stale", "sensor.site_bad_l3"),
    ]
