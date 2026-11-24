"""The pool's and the spa's shadow on a real site and their household controls (`pool/runtime.py`,
`api/pool_spa.py`, spec sections 5.3 and 10): `get_heat`'s `pool`, `spotnav/pool` and its webhook twin. Nothing is
written to a device."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

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


def _read_once(hass: HomeAssistant, shadow: Any, now: Any) -> None:
    """The circulation runs long enough for one valid water reading, then stops."""
    hass.states.async_set("switch.pool_cirkulation", "on")
    shadow.tick(now - timedelta(minutes=10))
    shadow.tick(now - timedelta(minutes=4))
    hass.states.async_set("switch.pool_cirkulation", "off")
    shadow.tick(now)


async def test_get_heat_shows_the_pool_watched_and_nothing_is_written(hass: HomeAssistant) -> None:
    calls: list = []
    hass.bus.async_listen("call_service", lambda event: calls.append(event))
    _charger, site, controller = await _site(hass)
    now = dt_util.now()
    _read_once(hass, controller.pool_spa, now)
    answer = await heat_answer(hass, site, writable=False)
    pool = answer["pool"]
    assert pool["season_open"] is True and pool["target_c"] == 27.0
    # The heat pump's own reading first, kept with its time once the circulation stopped.
    assert (pool["water_c"], pool["water_source"]) == (24.0, "heat_pump")
    assert pool["water_at"] == (now - timedelta(minutes=4)).isoformat()
    assert pool["heat_pump"] == {
        "on": True, "heating": False, "kw": 1.6, "live": False, "state": "watched", "reason": pool["heat_pump"]["reason"],
        "paused_until": None, "failures": 0,
    }
    assert pool["circulation"]["on"] is False and pool["circulation"]["state"] == "watched"
    # The day's need from the water: 24.0 / 3 in quarter hours.
    assert pool["today"]["need_h"] == 8.0 and pool["today"]["need_source"] == "water"
    assert set(pool) == {
        "season_open", "water_c", "water_source", "water_at", "target_c", "heat_pump", "circulation", "today", "planned", "extra",
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
    _read_once(hass, shadow, dt_util.now())
    shadow.set_extra(2, dt_util.utcnow())
    data = shadow._book_data()  # noqa: SLF001
    from custom_components.spotnav.pool.runtime import PoolSpaShadow

    again = PoolSpaShadow(hass, controller)
    again._restore(data, dt_util.now())  # noqa: SLF001
    assert again.extra == shadow.extra
    assert again.water_days == shadow.water_days and list(again.water_days.values())[0] == [24.0, 24.0]
    # The valid readings are kept too: the water is known again at the first look.
    assert again.readings == shadow.readings and set(again.readings) == {"heat_pump", "sensor"}


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


SPA = {"entity_id": "climate.spa", "family": "balboa", "baths": [{"weekdays": [1, 2, 3, 4, 5, 6, 7], "time": "19:00"}]}
SAUNA = {"entity_id": "climate.harvia", "family": "harvia_sauna", "lock_id": "binary_sensor.harvia_door"}


#: A fixed morning for the spa's and the sauna's tests: a bath every day at 19:00 is then hours ahead (on the real
#: clock the 19:00 bath was "not ready" for two hours a day and the answers' refusals changed).
DEADLINE_MORNING = "2026-07-01T08:00:00-07:00"


async def _deadline_site(hass: HomeAssistant, freezer: Any):
    freezer.move_to(DEADLINE_MORNING)
    charger, site, controller = await _site(hass, heat={"spa": SPA, "sauna": SAUNA})
    hass.states.async_set("climate.spa", "heat", {"current_temperature": 36.0, "temperature": 30.0, "hvac_action": "idle"})
    hass.states.async_set("climate.harvia", "off", {"current_temperature": 21.0, "hvac_action": "off"})
    hass.states.async_set("binary_sensor.harvia_door", "on")
    controller.pool_spa.tick(dt_util.now())
    return charger, site, controller


async def test_get_heat_shows_the_spa_and_the_sauna_watched(hass: HomeAssistant, freezer: Any) -> None:
    _charger, site, _controller = await _deadline_site(hass, freezer)
    answer = await heat_answer(hass, site, writable=False)
    spa, sauna = answer["spa"], answer["sauna"]
    assert spa["family"] == "balboa" and spa["water_c"] == 36.0 and spa["bath_c"] == 38.0
    assert spa["next_bath"]["source"] == "weekly" and spa["rate_learned"] is False and spa["rate_c_h"] == 1.5
    assert spa["control"] == {"state": "watched", "reason": None, "paused_until": None, "failures": 0} and spa["live"] is False
    assert set(spa) == {
        "family", "water_c", "bath_c", "idle_c", "floor_c", "heating", "kw", "rate_c_h", "rate_learned", "state",
        "ready_at", "next_bath", "baths", "bath_once", "bath_now", "live", "control", "would", "not_ready", "planned",
        "notes",
    }
    assert sauna["family"] == "harvia_sauna" and sauna["temp_c"] == 21.0 and sauna["locked"] is True
    assert sauna["next_session"] is None and sauna["would"]["action"] == "off" and sauna["would"]["reason"] == "no_session"
    assert sauna["control"]["reason"] == "cloud_slow"


async def test_the_spas_and_the_saunas_controls(hass: HomeAssistant, freezer: Any) -> None:
    from custom_components.spotnav.api.pool_spa import deadline_answer

    _charger, site, controller = await _deadline_site(hass, freezer)
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


async def test_the_spa_and_sauna_webhook_twins(hass: HomeAssistant, freezer: Any) -> None:
    import json

    charger, _site_entry, _controller = await _deadline_site(hass, freezer)
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


async def test_the_pool_s_opt_ins_show_in_get_heat_and_the_shed_order_s_offers(hass: HomeAssistant) -> None:
    from custom_components.spotnav.pool.rules import LOAD_POOL_CIRCULATION  # noqa: PLC0415

    _charger, site, controller = await _site(hass, {"pool": {**POOL, "control_circulation": True}})
    now = dt_util.utcnow()
    controller.pool_spa.tick(now)
    await controller.pool_spa_control.async_look(now)
    answer = await heat_answer(hass, site, writable=False)
    circulation = answer["pool"]["circulation"]
    assert circulation["state"] == "controlled" and circulation["live"] is True and circulation["failures"] == 0
    assert answer["pool"]["heat_pump"]["state"] == "watched"
    hass.states.async_set("switch.pool_cirkulation", "on")
    controller.pool_spa_control.pool.circ_on_since = now - timedelta(hours=1)
    await controller.pool_spa_control.async_look(now)
    offers = controller.pool_spa.shed_offers(now)
    assert offers[LOAD_POOL_CIRCULATION].live is True


async def test_without_prices_the_day_s_filtering_still_runs(hass: HomeAssistant, monkeypatch) -> None:
    _charger, _site_entry, controller = await _site(hass)
    shadow = controller.pool_spa
    monkeypatch.setattr(shadow, "_prices", lambda _now: [])
    now = dt_util.utcnow()
    shadow.tick(now)
    # Nothing filtered today yet: the need is run in any hour, never read as off (review S5).
    assert shadow.would == {"action": "filter", "until": None, "reason": "need"}
    assert shadow.would_at == now


async def test_a_water_reading_counts_only_once_the_circulation_has_run_five_minutes(hass: HomeAssistant) -> None:
    """Both readings sit on the pipe: a reading is the pool's only after the circulation has run five minutes, the last
    valid one is kept with its time, the heat pump's first; a cooled pipe never reaches the day's water."""
    _charger, _site_entry, controller = await _site(hass)
    shadow = controller.pool_spa
    start = dt_util.now()
    shadow.tick(start)
    assert (shadow.water_c, shadow.water_source, shadow.water_at) == (None, None, None)
    hass.states.async_set("switch.pool_cirkulation", "on")
    shadow.tick(start + timedelta(minutes=1))
    shadow.tick(start + timedelta(minutes=5))
    assert shadow.water_c is None
    settled = start + timedelta(minutes=6)
    shadow.tick(settled)
    assert (shadow.water_c, shadow.water_source, shadow.water_at) == (24.0, "heat_pump", settled)
    # The heat pump's reading lost: the standalone sensor's, read at the same look, stands in.
    hass.states.async_set("climate.poolpump", "unavailable")
    later = start + timedelta(minutes=7)
    shadow.tick(later)
    assert (shadow.water_c, shadow.water_source, shadow.water_at) == (24.6, "sensor", later)
    hass.states.async_set("climate.poolpump", "heat", {"hvac_action": "idle", "current_temperature": 24.2})
    shadow.tick(start + timedelta(minutes=8))
    assert (shadow.water_c, shadow.water_source) == (24.2, "heat_pump")
    # The circulation stops: both pipes drift towards the air; the last valid reading is kept with its time.
    hass.states.async_set("switch.pool_cirkulation", "off")
    hass.states.async_set("climate.poolpump", "heat", {"hvac_action": "idle", "current_temperature": 15.0})
    hass.states.async_set("sensor.pool_vatten", "14.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    shadow.tick(start + timedelta(minutes=30))
    assert (shadow.water_c, shadow.water_source, shadow.water_at) == (24.2, "heat_pump", start + timedelta(minutes=8))
    assert all(low >= 24.0 for low, _high in shadow.water_days.values())
    # Started again: nothing counts for five minutes.
    hass.states.async_set("switch.pool_cirkulation", "on")
    shadow.tick(start + timedelta(minutes=31))
    shadow.tick(start + timedelta(minutes=34))
    assert shadow.water_c == 24.2


async def test_without_a_circulation_pump_the_readings_count_as_they_come(hass: HomeAssistant) -> None:
    _charger, _site_entry, controller = await _site(hass, heat={"pool": {**POOL, "circulation_id": None}})
    shadow = controller.pool_spa
    now = dt_util.now()
    shadow.tick(now)
    assert (shadow.water_c, shadow.water_source, shadow.water_at) == (24.0, "heat_pump", now)
    # A sensor alone (no heat pump reading): the sensor's.
    hass.states.async_set("climate.poolpump", "heat", {"hvac_action": "idle"})
    shadow.tick(now + timedelta(minutes=1))
    assert (shadow.water_c, shadow.water_source) == (24.6, "sensor")


async def test_core_tuyas_heat_pump_reads_its_own_water_sensor(hass: HomeAssistant) -> None:
    """The switch-and-number heat pump's own reading is the `temp_current` sensor on its switch's device."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    _charger, site, controller = await _site(hass, heat={})
    device = dr.async_get(hass).async_get_or_create(config_entry_id=site.entry_id, identifiers={("tuya", "abc")})
    registry = er.async_get(hass)
    switch = registry.async_get_or_create("switch", "tuya", "abcswitch", device_id=device.id, suggested_object_id="pool_hp")
    water = registry.async_get_or_create("sensor", "tuya", "abctemp_current", device_id=device.id, suggested_object_id="pool_hp_temp")
    hass.states.async_set(switch.entity_id, "on")
    hass.states.async_set(water.entity_id, "23.5", {"unit_of_measurement": "°C", "device_class": "temperature"})
    pool = {"heat_pump": {"entity_id": switch.entity_id, "shape": "switch_number", "switch_id": switch.entity_id}}
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: {"pool": pool}})
    await hass.async_block_till_done()
    shadow = controller.pool_spa
    shadow.tick(dt_util.now())
    assert (shadow.water_c, shadow.water_source) == (23.5, "heat_pump")
