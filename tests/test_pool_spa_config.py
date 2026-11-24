"""The pool's and the spa's settings blocks and the heat card's phones (`pool/config.py`, `spa/config.py`,
`notifications/heat_settings.py`), inside the heat settings (`heat/config.py`): read leniently, written strictly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.spotnav.heat.config import HeatConfig, apply_changes, read_config
from custom_components.spotnav.notifications.heat_settings import HeatNotifications
from custom_components.spotnav.pool.config import PoolConfig, PoolHeatPump, read_pool
from custom_components.spotnav.sauna.config import SaunaConfig, read_sauna
from custom_components.spotnav.spa.config import Bath, SpaConfig, idle_from_setpoint, read_spa

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
KNOWN = {
    "climate.poolpump": "pool_heat_pump",
    "climate.air": "climate",
    "switch.pool_cirkulation": "switch",
    "switch.tuya_pool_switch": "switch",
    "number.tuya_pool_temp_set": "number",
    "sensor.tuya_pool_temp_current": "sensor",
    "sensor.pool_vatten": "temperature",
    "sensor.power": "power",
    "climate.spa": "spa",
    "water_heater.gecko": "spa",
}


def check(entity_id: str, role: str) -> bool | None:
    kind = KNOWN.get(entity_id)
    if kind is None:
        return None
    if role == "pool_water":
        return kind == "temperature"
    return True


def write(changes: dict, current: HeatConfig | None = None, setpoint: float | None = None):
    return apply_changes(current or HeatConfig(), changes, now=NOW, check=check, setpoint_of=lambda _e: setpoint)


def codes(errors) -> list[tuple[str, str]]:
    return [(error.field, error.code) for error in errors]


# ---- the pool


def test_a_new_pool_has_the_defaults_and_starts_watched() -> None:
    config, errors = write({"pool": {"heat_pump": {"entity_id": "climate.poolpump"}, "circulation_id": "switch.pool_cirkulation"}})
    assert errors == []
    assert config.pool == PoolConfig(heat_pump=PoolHeatPump("climate.poolpump"), circulation_id="switch.pool_cirkulation")
    assert config.pool.target_c == 27.0 and config.pool.season_open and config.pool.filter_hours is None
    assert not config.pool.control_heat_pump and not config.pool.control_circulation
    # Stored and read back the same.
    assert read_config(config.as_dict()) == config


def test_the_pool_ranges_and_steps() -> None:
    base, _ = write({"pool": {"circulation_id": "switch.pool_cirkulation"}})
    for changes, expected in (
        ({"target_c": 17.5}, ("pool.target_c", "out_of_range")),
        ({"target_c": 27.3}, ("pool.target_c", "not_a_step")),
        ({"target_c": "27"}, ("pool.target_c", "invalid_value")),
        ({"filter_hours": 0.5}, ("pool.filter_hours", "out_of_range")),
        ({"filter_hours": 3.2}, ("pool.filter_hours", "not_a_step")),
        ({"heat_pump_kw": 20}, ("pool.heat_pump_kw", "out_of_range")),
        ({"circulation_kw": 0.01}, ("pool.circulation_kw", "out_of_range")),
        ({"season_open": "yes"}, ("pool.season_open", "invalid_value")),
        ({"water_sensor": "sensor.power"}, ("pool.water_sensor", "wrong_kind")),
        ({"water_sensor": "sensor.nothing"}, ("pool.water_sensor", "entity_not_found")),
        ({"circulation_id": "climate.air"}, ("pool.circulation_id", "wrong_kind")),
        ({"colour": "blue"}, ("pool.colour", "unknown_field")),
    ):
        config, errors = write({"pool": changes}, base)
        assert codes(errors) == [expected], changes
        assert config == base
    config, errors = write({"pool": {"target_c": 28.5, "filter_hours": 3.5, "heat_pump_kw": 1.6, "circulation_kw": 0.4}}, base)
    assert errors == [] and config.pool is not None
    assert (config.pool.target_c, config.pool.filter_hours, config.pool.heat_pump_kw, config.pool.circulation_kw) == (28.5, 3.5, 1.6, 0.4)
    config, errors = write({"pool": {"filter_hours": None}}, config)
    assert errors == [] and config.pool.filter_hours is None


def test_an_opt_in_needs_its_entities_and_the_heat_pump_needs_the_circulation() -> None:
    pump_only, _ = write({"pool": {"heat_pump": {"entity_id": "climate.poolpump"}}})
    config, errors = write({"pool": {"control_heat_pump": True}}, pump_only)
    assert codes(errors) == [("pool.control_heat_pump", "entity_required")]
    config, errors = write({"pool": {"control_circulation": True}}, pump_only)
    assert codes(errors) == [("pool.control_circulation", "entity_required")]
    both, errors = write({"pool": {"circulation_id": "switch.pool_cirkulation", "control_heat_pump": True}}, pump_only)
    assert errors == [] and both.pool.control_heat_pump
    # Taking the circulation away takes the heat pump's opt-in with it (the interlock).
    config, errors = write({"pool": {"circulation_id": None}}, both)
    assert errors == [] and not config.pool.control_heat_pump
    # Another heat pump starts watched again.
    config, errors = write({"pool": {"heat_pump": {"entity_id": "climate.air"}}}, both)
    assert errors == [] and not config.pool.control_heat_pump


def test_a_pool_needs_a_heat_pump_or_a_circulation_and_null_removes_it() -> None:
    config, errors = write({"pool": {"target_c": 28}})
    assert codes(errors) == [("pool", "nothing_chosen")]
    pool, _ = write({"pool": {"circulation_id": "switch.pool_cirkulation"}})
    config, errors = write({"pool": None}, pool)
    assert errors == [] and config.pool is None


def test_core_tuyas_heat_pump_needs_its_switch() -> None:
    config, errors = write({"pool": {"heat_pump": {"entity_id": "sensor.tuya_pool_temp_current", "shape": "switch_number"}}})
    assert codes(errors) == [("pool.heat_pump", "entity_required")]
    config, errors = write(
        {
            "pool": {
                "heat_pump": {
                    "entity_id": "sensor.tuya_pool_temp_current",
                    "shape": "switch_number",
                    "switch_id": "switch.tuya_pool_switch",
                    "number_id": "number.tuya_pool_temp_set",
                }
            }
        }
    )
    assert errors == []
    assert config.pool.heat_pump == PoolHeatPump(
        "sensor.tuya_pool_temp_current", "switch_number", "switch.tuya_pool_switch", "number.tuya_pool_temp_set"
    )
    assert read_config(config.as_dict()) == config


def test_the_circulation_is_never_the_heat_pumps_own_switch() -> None:
    config, errors = write(
        {
            "pool": {
                "heat_pump": {"entity_id": "sensor.tuya_pool_temp_current", "shape": "switch_number", "switch_id": "switch.tuya_pool_switch"},
                "circulation_id": "switch.tuya_pool_switch",
            }
        }
    )
    assert codes(errors) == [("pool.circulation_id", "duplicate")]


def test_a_broken_stored_pool_reads_as_its_defaults() -> None:
    assert read_pool(None) is None
    assert read_pool({"target_c": 30}) is None
    pool = read_pool(
        {
            "circulation_id": "switch.pool_cirkulation",
            "target_c": 99,
            "filter_hours": "x",
            "season_open": "no",
            "control_heat_pump": True,
            "heat_pump": {"entity_id": "light.x"},
        }
    )
    assert pool == PoolConfig(circulation_id="switch.pool_cirkulation")


# ---- the spa


def test_a_new_spa_starts_watched_at_30_or_the_lower_setpoint_found() -> None:
    config, errors = write({"spa": {"entity_id": "climate.spa", "family": "balboa"}}, setpoint=36.7)
    assert errors == []
    assert config.spa == SpaConfig("climate.spa", "balboa", idle_c=30.0)
    # A spa found below 30 °C keeps its own setpoint as the idle temperature.
    lower, _ = write({"spa": {"entity_id": "climate.spa", "family": "balboa"}}, setpoint=27.8)
    assert lower.spa.idle_c == 28.0
    assert (config.spa.bath_c, config.spa.floor_c, config.spa.control) == (38.0, 10.0, False)
    assert read_config(config.as_dict()) == config
    # Without a setpoint: 30 °C.
    config, _ = write({"spa": {"entity_id": "climate.spa"}})
    assert config.spa.idle_c == 30.0 and config.spa.family == "generic"
    assert idle_from_setpoint(5.0) == 10.0 and idle_from_setpoint(55.0) == 30.0


def test_the_spa_ranges_baths_and_one_off_bath() -> None:
    base, _ = write({"spa": {"entity_id": "climate.spa", "family": "balboa"}}, setpoint=30.0)
    for changes, expected in (
        ({"bath_c": 41}, ("spa.bath_c", "out_of_range")),
        ({"idle_c": 30.2}, ("spa.idle_c", "not_a_step")),
        ({"floor_c": 4}, ("spa.floor_c", "out_of_range")),
        ({"heater_kw": 0.1}, ("spa.heater_kw", "out_of_range")),
        ({"baths": [{"weekdays": [8], "time": "19:00"}]}, ("spa.baths", "invalid_value")),
        ({"baths": [{"weekdays": [0], "time": "19:00"}]}, ("spa.baths", "invalid_value")),
        ({"baths": [{"weekdays": [1], "time": "25:00"}]}, ("spa.baths", "invalid_value")),
        ({"baths": [{"weekdays": [], "time": "19:00"}]}, ("spa.baths", "invalid_value")),
        ({"baths": [{"weekdays": [1, 1], "time": "19:00"}]}, ("spa.baths", "invalid_value")),
        ({"baths": [{"weekdays": [1], "time": "19:00"}] * 2}, ("spa.baths", "duplicate")),
        ({"baths": [{"weekdays": [d], "time": "19:00"} for d in range(1, 6)]}, ("spa.baths", "too_many")),
        ({"bath_once": (NOW - timedelta(hours=1)).isoformat()}, ("spa.bath_once", "bath_in_past")),
        ({"bath_once": (NOW + timedelta(days=31)).isoformat()}, ("spa.bath_once", "bath_too_far")),
        ({"bath_once": "tomorrow"}, ("spa.bath_once", "invalid_value")),
        ({"family": "jacuzzi"}, ("spa.family", "invalid_value")),
        ({"entity_id": "switch.pool_cirkulation"}, ("spa.entity_id", "wrong_kind")),
    ):
        config, errors = write({"spa": changes}, base)
        assert codes(errors) == [expected], changes
        assert config == base
    once = (NOW + timedelta(days=1, hours=7)).isoformat()
    config, errors = write(
        {"spa": {"baths": [{"weekdays": [6, 5], "time": "19:00"}], "bath_once": once, "bath_c": 38.5, "idle_c": 32}}, base
    )
    assert errors == []
    assert config.spa.baths == (Bath((5, 6), "19:00"),) and config.spa.bath_once == once
    assert (config.spa.bath_c, config.spa.idle_c) == (38.5, 32.0)


def test_a_watch_only_family_cannot_be_controlled_and_another_spa_starts_watched() -> None:
    config, errors = write({"spa": {"entity_id": "climate.spa", "family": "generic", "control": True}})
    assert codes(errors) == [("spa.control", "watch_only")]
    controlled, errors = write({"spa": {"entity_id": "climate.spa", "family": "balboa", "control": True}})
    assert errors == [] and controlled.spa.control
    config, errors = write({"spa": {"entity_id": "water_heater.gecko", "family": "gecko"}}, controlled, setpoint=35.0)
    assert errors == [] and not config.spa.control and config.spa.idle_c == 30.0


def test_a_broken_stored_spa_reads_as_its_defaults() -> None:
    assert read_spa({"family": "balboa"}) is None
    spa = read_spa(
        {"entity_id": "climate.spa", "family": "generic", "control": True, "bath_c": 50, "baths": [{"weekdays": [9], "time": "x"}]}
    )
    assert spa == SpaConfig("climate.spa")


def test_stored_weekdays_in_the_first_numbering_read_as_iso_numbers() -> None:
    # The first builds stored Monday 0 to Sunday 6: a list holding a 0 is moved up by one, an ISO list is kept.
    spa = read_spa({"entity_id": "climate.spa", "baths": [{"weekdays": [0, 6], "time": "19:00"}, {"weekdays": [5, 7], "time": "08:00"}]})
    assert spa is not None and spa.baths == (Bath((1, 7), "19:00"), Bath((5, 7), "08:00"))
    sauna = read_sauna({"entity_id": "climate.huum", "family": "huum", "sessions": [{"weekdays": [0, 4], "time": "17:00"}]})
    assert sauna is not None and sauna.sessions == (Bath((1, 5), "17:00"),)


# ---- the phones and who may write


def test_the_heat_cards_phones() -> None:
    assert HeatConfig().heat_notifications == HeatNotifications((), ("plan_at_risk", "pool_season"))
    config, errors = write({"heat_notifications": {"targets": ["mobile_app_pixel"], "events": ["pool_season"]}})
    assert errors == [] and config.heat_notifications == HeatNotifications(("mobile_app_pixel",), ("pool_season",))
    assert config.heat_notifications.wants("pool_season") and not config.heat_notifications.wants("plan_at_risk")
    for raw in ({"targets": ["Not A Service"]}, {"events": ["charge_complete"]}, {"targets": ["a", "a"]}, {"phones": []}):
        _config, errors = write({"heat_notifications": raw})
        assert len(errors) == 1 and errors[0].field == "heat_notifications", raw
    assert read_config({"heat_notifications": {"targets": ["mobile_app_x"], "events": ["pool_season", "later_event"]}}).heat_notifications == (
        HeatNotifications(("mobile_app_x",), ("pool_season",))
    )


def test_a_pool_spa_or_phones_write_is_an_administrators(hass) -> None:
    from types import SimpleNamespace

    import pytest

    from custom_components.spotnav.api.heat import HeatWriteRefusal, write_heat

    site = SimpleNamespace(data={}, entry_id="site")
    for changes in ({"pool": None}, {"spa": None}, {"heat_notifications": None}):
        with pytest.raises(HeatWriteRefusal) as refused:
            write_heat(hass, site, changes, None, admin=False)  # type: ignore[arg-type]
        assert refused.value.code == "spotnav_not_admin"
        with pytest.raises(HeatWriteRefusal) as refused:
            write_heat(hass, site, changes, None, admin=False, over_webhook=True)  # type: ignore[arg-type]
        assert refused.value.code == "spotnav_not_permitted_over_webhook"


def test_the_compare_and_set_compares_the_blocks() -> None:
    from custom_components.spotnav.api.heat import _same

    pool, _ = write({"pool": {"circulation_id": "switch.pool_cirkulation"}})
    stored = pool.as_dict()["pool"]
    assert _same("pool", stored, dict(stored))
    assert not _same("pool", stored, {**stored, "target_c": 28.0})
    assert _same("pool", stored, {**stored, "target_c": 27})


# ---- the sauna


def test_a_sauna_from_an_integration_and_its_ranges() -> None:
    known = {**KNOWN, "climate.huum": "climate", "switch.bastu": "switch", "binary_sensor.bastu_door": "door"}

    def sauna_check(entity_id: str, role: str) -> bool | None:
        return True if entity_id in known else None

    config, errors = apply_changes(
        HeatConfig(),
        {"sauna": {"entity_id": "climate.huum", "family": "huum", "sessions": [{"weekdays": [6], "time": "17:00"}], "lock_id": "binary_sensor.bastu_door"}},
        now=NOW,
        check=sauna_check,
    )
    assert errors == []
    assert config.sauna == SaunaConfig(
        "climate.huum", "huum", sessions=(Bath((6,), "17:00"),), lock_id="binary_sensor.bastu_door"
    )
    assert config.sauna.target_c == 80.0 and config.sauna.controllable
    assert read_config(config.as_dict()) == config
    for changes, expected in (
        ({"target_c": 120}, ("sauna.target_c", "out_of_range")),
        ({"target_c": 80.5}, ("sauna.target_c", "not_a_step")),
        ({"heater_kw": 1}, ("sauna.heater_kw", "out_of_range")),
        ({"session_once": (NOW - timedelta(hours=1)).isoformat()}, ("sauna.session_once", "session_in_past")),
        ({"lock_id": "sensor.x"}, ("sauna.lock_id", "wrong_kind")),
        ({"entity_id": "switch.bastu"}, ("sauna.entity_id", "wrong_kind")),
    ):
        _same, found = apply_changes(config, {"sauna": changes}, now=NOW, check=sauna_check)
        assert [(e.field, e.code) for e in found] == [expected], changes


def test_a_relay_sauna_is_controlled_only_with_its_safety_measure_declared() -> None:
    def sauna_check(entity_id: str, role: str) -> bool | None:
        return True

    relay = {"entity_id": "switch.bastu", "family": "relay", "heater_kw": 9}
    config, errors = apply_changes(HeatConfig(), {"sauna": {**relay, "control": True}}, now=NOW, check=sauna_check)
    assert [(e.field, e.code) for e in errors] == [("sauna.control", "safety_required")]
    config, errors = apply_changes(
        HeatConfig(), {"sauna": {**relay, "safety_declared": True, "control": True}}, now=NOW, check=sauna_check
    )
    assert errors == [] and config.sauna.control and config.sauna.controllable
    # A generic climate is watched only.
    _config, errors = apply_changes(
        HeatConfig(), {"sauna": {"entity_id": "climate.x", "family": "generic", "control": True}}, now=NOW, check=sauna_check
    )
    assert [(e.field, e.code) for e in errors] == [("sauna.control", "watch_only")]
    # Stored broken: watched, undeclared.
    assert read_sauna({"entity_id": "switch.bastu", "family": "relay", "control": True}) == SaunaConfig("switch.bastu", "relay")
