"""The pool's and the spa's shadow on a real site and their household controls (`pool/runtime.py`,
`api/pool_spa.py`, spec sections 5.3 and 10): `get_heat`'s `pool`, `spotnav/pool` and its webhook twin. Nothing is
written to a device."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.api.heat import heat_answer, stored_config
from custom_components.spotnav.api.pool_spa import pool_answer
from custom_components.spotnav.api.site_webhook import SITE_ACTIONS
from custom_components.spotnav.const import CONF_HEAT
from custom_components.spotnav.runtime import site_controller_for

from tests.world import setup_charger_and_site

POOL = {
    "heat_pump": {"entity_id": "climate.poolpump"},
    "circulation_id": "switch.pool_cirkulation",
    "water_sensor": "sensor.pool_vatten",
    "heat_pump_kw": 1.6,
    "circulation_kw": 0.4,
}


async def _site(hass: HomeAssistant, heat: dict | None = None):
    charger, site = await setup_charger_and_site(hass)
    assert site is not None
    hass.states.async_set("climate.poolpump", "heat", {"hvac_action": "idle", "current_temperature": 24.0})
    hass.states.async_set("switch.pool_cirkulation", "off")
    hass.states.async_set("sensor.pool_vatten", "24.6", {"unit_of_measurement": "°C", "device_class": "temperature"})
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: heat if heat is not None else {"pool": POOL}})
    await hass.async_block_till_done()
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    return charger, site, controller


async def test_get_heat_shows_the_pool_watched_and_nothing_is_written(hass: HomeAssistant) -> None:
    calls: list = []
    hass.bus.async_listen("call_service", lambda event: calls.append(event))
    _charger, site, controller = await _site(hass)
    controller.pool_spa.tick(dt_util.now())
    answer = await heat_answer(hass, site, writable=False)
    pool = answer["pool"]
    assert pool["season_open"] is True and pool["target_c"] == 27.0
    assert (pool["water_c"], pool["water_source"]) == (24.6, "sensor")
    assert pool["heat_pump"] == {"on": True, "heating": False, "kw": 1.6, "live": False, "state": "watched", "reason": pool["heat_pump"]["reason"]}
    assert pool["circulation"]["on"] is False and pool["circulation"]["state"] == "watched"
    # The day's need from the water: 24.6 / 3 in quarter hours.
    assert pool["today"]["need_h"] == 8.25 and pool["today"]["need_source"] == "water"
    assert set(pool) == {
        "season_open", "water_c", "water_source", "target_c", "heat_pump", "circulation", "today", "planned", "extra",
        "season_hint", "would", "notes",
    }
    assert [event for event in calls if event.data.get("domain") in ("switch", "climate")] == []
    # A site without a pool answers `null`.
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: {}})
    assert (await heat_answer(hass, site, writable=False))["pool"] is None


async def test_the_pools_controls_for_anyone_who_sees_the_card(hass: HomeAssistant) -> None:
    _charger, site, controller = await _site(hass)
    answer = pool_answer(hass, site, {"action": "target", "target_c": 28.5})
    assert answer["ok"] and answer["pool"]["target_c"] == 28.5
    assert stored_config(site).pool.target_c == 28.5  # type: ignore[union-attr]
    assert pool_answer(hass, site, {"action": "target", "target_c": 45})["error"] == "spotnav_invalid_value"
    extra = pool_answer(hass, site, {"action": "extra_filtering", "hours": 4})
    assert extra["ok"] and extra["pool"]["extra"]["hours"] == 4
    until = dt_util.parse_datetime(extra["pool"]["extra"]["until"])
    assert until is not None and timedelta(hours=23) < until - dt_util.utcnow() <= timedelta(hours=24)
    assert pool_answer(hass, site, {"action": "extra_filtering", "hours": 3})["error"] == "spotnav_invalid_value"
    # Closing the season ends the extra filtering; SpotNav touches nothing then.
    closed = pool_answer(hass, site, {"action": "season", "open": False})
    assert closed["ok"] and closed["pool"]["season_open"] is False and closed["pool"]["extra"] is None
    assert closed["pool"]["would"] is None and controller.pool_spa.extra is None
    assert pool_answer(hass, site, {"action": "extra_filtering", "hours": 2})["error"] == "spotnav_invalid_value"
    assert pool_answer(hass, site, {"action": "season", "open": True})["pool"]["season_open"] is True
    assert pool_answer(hass, site, {"action": "dive"})["error"] == "spotnav_invalid_value"


async def test_no_pool_is_not_supported(hass: HomeAssistant) -> None:
    _charger, site, _controller = await _site(hass, heat={})
    assert pool_answer(hass, site, {"action": "target", "target_c": 28})["error"] == "spotnav_not_supported"


async def test_the_webhook_twin(hass: HomeAssistant) -> None:
    import json

    charger, site, _controller = await _site(hass)
    response = await SITE_ACTIONS["pool"](hass, charger, {"pool_action": "extra_filtering", "hours": 2})
    body = json.loads(response.body)
    assert response.status == 200 and body["ok"] and body["action"] == "pool" and body["pool"]["extra"]["hours"] == 2
    refused = await SITE_ACTIONS["pool"](hass, charger, {"pool_action": "target", "target_c": 10})
    assert refused.status == 400 and json.loads(refused.body)["error"] == "spotnav_invalid_value"


async def test_the_shadow_keeps_its_days_and_extra_over_a_restart(hass: HomeAssistant) -> None:
    _charger, site, controller = await _site(hass)
    shadow = controller.pool_spa
    shadow.tick(dt_util.now())
    shadow.set_extra(2, dt_util.utcnow())
    data = shadow._book_data()  # noqa: SLF001
    from custom_components.spotnav.pool.runtime import PoolSpaShadow

    again = PoolSpaShadow(hass, controller)
    again._restore(data, dt_util.now())  # noqa: SLF001
    assert again.extra == shadow.extra
    assert again.water_days == shadow.water_days and list(again.water_days.values())[0] == [24.6, 24.6]
