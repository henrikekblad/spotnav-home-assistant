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


async def test_the_shadow_counts_each_loads_energy_per_hour(hass: HomeAssistant) -> None:
    """The circulation and the heat pump as they really run, at their entered powers, into their UTC hours; a gap over
    two minutes is not counted; the hours are kept over a restart."""
    _charger, _site_entry, controller = await _site(hass)
    shadow = controller.pool_spa
    start = dt_util.utcnow().replace(minute=59, second=0, microsecond=0)
    shadow.tick(start)
    hass.states.async_set("switch.pool_cirkulation", "on")
    hass.states.async_set("climate.poolpump", "heat", {"hvac_action": "heating", "current_temperature": 24.0})
    shadow.tick(start + timedelta(minutes=2))
    hours = shadow.energy_hours("pool")
    first = start.replace(minute=0)
    # One minute in each hour at 0.4 + 1.6 kW.
    assert hours == {first: 2.0 / 60, first + timedelta(hours=1): 2.0 / 60}
    shadow.tick(start + timedelta(minutes=10))
    assert shadow.energy_hours("pool") == hours
    from custom_components.spotnav.pool.runtime import PoolSpaShadow

    again = PoolSpaShadow(hass, controller)
    again._restore(shadow._book_data(), dt_util.now())  # noqa: SLF001
    assert again.energy_hours("pool") == {moment: round(kwh, 4) for moment, kwh in hours.items()}


SPA = {"entity_id": "climate.spa", "family": "balboa", "baths": [{"weekdays": [0, 1, 2, 3, 4, 5, 6], "time": "19:00"}]}
SAUNA = {"entity_id": "climate.harvia", "family": "harvia_sauna", "lock_id": "binary_sensor.harvia_door"}


async def _deadline_site(hass: HomeAssistant):
    charger, site, controller = await _site(hass, heat={"spa": SPA, "sauna": SAUNA})
    hass.states.async_set("climate.spa", "heat", {"current_temperature": 36.0, "temperature": 30.0, "hvac_action": "idle"})
    hass.states.async_set("climate.harvia", "off", {"current_temperature": 21.0, "hvac_action": "off"})
    hass.states.async_set("binary_sensor.harvia_door", "on")
    controller.pool_spa.tick(dt_util.now())
    return charger, site, controller


async def test_get_heat_shows_the_spa_and_the_sauna_watched(hass: HomeAssistant) -> None:
    _charger, site, _controller = await _deadline_site(hass)
    answer = await heat_answer(hass, site, writable=False)
    spa, sauna = answer["spa"], answer["sauna"]
    assert spa["family"] == "balboa" and spa["water_c"] == 36.0 and spa["bath_c"] == 38.0
    assert spa["next_bath"]["source"] == "weekly" and spa["rate_learned"] is False and spa["rate_c_h"] == 1.5
    assert spa["control"] == {"state": "watched", "reason": None, "paused_until": None} and spa["live"] is False
    assert set(spa) == {
        "family", "water_c", "bath_c", "idle_c", "floor_c", "heating", "kw", "rate_c_h", "rate_learned", "state",
        "ready_at", "next_bath", "baths", "bath_once", "bath_now", "live", "control", "would", "not_ready", "planned",
        "notes",
    }
    assert sauna["family"] == "harvia_sauna" and sauna["temp_c"] == 21.0 and sauna["locked"] is True
    assert sauna["next_session"] is None and sauna["would"]["action"] == "off" and sauna["would"]["reason"] == "no_session"
    assert sauna["control"]["reason"] == "cloud_slow"


async def test_the_spas_and_the_saunas_controls(hass: HomeAssistant) -> None:
    from custom_components.spotnav.api.pool_spa import deadline_answer

    _charger, site, controller = await _deadline_site(hass)
    assert deadline_answer(hass, site, "spa", {"action": "bath_c", "value": 39})["spa"]["bath_c"] == 39.0
    assert deadline_answer(hass, site, "spa", {"action": "bath_c", "value": 45})["error"] == "spotnav_invalid_value"
    now = deadline_answer(hass, site, "spa", {"action": "bath_now"})
    assert now["ok"] and now["spa"]["bath_now"] is not None and now["spa"]["next_bath"]["source"] == "now"
    # The day band's runs: heating from now, then the hold, each with its words' reason.
    planned = now["spa"]["planned"]
    assert planned and all(item["what"] in ("heat", "hold") for item in planned)
    assert planned[0]["reason"] == "now" and planned[-1]["what"] == "hold"
    assert deadline_answer(hass, site, "spa", {"action": "bath_now"})["error"] == "spotnav_rate_limited"
    assert deadline_answer(hass, site, "spa", {"action": "cancel_now"})["spa"]["bath_now"] is None
    # Nothing that does not fit: the answers to it are refused.
    assert deadline_answer(hass, site, "spa", {"action": "take_what_fits"})["error"] == "spotnav_invalid_value"
    once = (dt_util.utcnow() + timedelta(hours=5)).replace(microsecond=0).isoformat()
    booked = deadline_answer(hass, site, "sauna", {"action": "session_once", "value": once})
    assert booked["ok"] and booked["sauna"]["session_once"] is not None and booked["sauna"]["next_session"]["source"] == "once"
    # The door is open: a heat-up would be blocked, never started past the lock.
    started = deadline_answer(hass, site, "sauna", {"action": "session_now"})
    assert started["sauna"]["would"]["action"] == "blocked" and started["sauna"]["would"]["reason"] == "lock"
    hass.states.async_set("binary_sensor.harvia_door", "off")
    controller.pool_spa.tick(dt_util.now())
    assert (await heat_answer(hass, site, writable=False))["sauna"]["would"]["action"] == "heat"
    assert deadline_answer(hass, site, "sauna", {"action": "fly"})["error"] == "spotnav_invalid_value"
    assert deadline_answer(hass, site, "sauna", {"action": "idle_c", "value": 30})["error"] == "spotnav_invalid_value"


async def test_the_spa_and_sauna_webhook_twins(hass: HomeAssistant) -> None:
    import json

    charger, _site_entry, _controller = await _deadline_site(hass)
    response = await SITE_ACTIONS["spa"](hass, charger, {"spa_action": "idle_c", "value": 32})
    assert response.status == 200 and json.loads(response.body)["spa"]["idle_c"] == 32.0
    response = await SITE_ACTIONS["sauna"](hass, charger, {"sauna_action": "target_c", "value": 85})
    assert response.status == 200 and json.loads(response.body)["sauna"]["target_c"] == 85.0
    refused = await SITE_ACTIONS["sauna"](hass, charger, {"sauna_action": "target_c", "value": 200})
    assert refused.status == 400


async def test_without_a_spa_or_sauna_the_controls_are_not_supported(hass: HomeAssistant) -> None:
    from custom_components.spotnav.api.pool_spa import deadline_answer

    _charger, site, _controller = await _site(hass)
    assert deadline_answer(hass, site, "spa", {"action": "bath_now"})["error"] == "spotnav_not_supported"
    assert deadline_answer(hass, site, "sauna", {"action": "session_now"})["error"] == "spotnav_not_supported"
