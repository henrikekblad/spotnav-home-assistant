"""Phase switches of Lektrico, NRGkick, go-e (API v2 and MQTT) and Fronius Wattpilot (`PhaseSwitch`), from each
integration's real entity shapes.

* Lektrico (core, HA 2026.9): `switch.*_force_single_phase` (key and translation key `force_single_phase`, unique id
  `<serial>_force_single_phase`), created only on the three-phase model `3p22k`; on = one phase. Its `limit_reason`
  sensor (unique id `<serial>_limit_reason`) says `switching_phases` while it switches.
* NRGkick (core, HA 2026.9): `number.*_phase_count` (translation key `phase_count`, unique id `<serial>_phase_count`),
  1 to 3, its maximum the attached connector's phase count.
* go-e API v2 (marq24/ha-goecharger-api2 2026.10.8): `select` `psm` (unique id `goecharger_api2.goe_<serial>_psm`,
  translation key `psm`, options `0` auto, `1` one phase, `2` three phases); `modelstatus_value` (worded, enabled) and
  `modelstatus` (code, disabled by default) say "not charging because of phase switch" (23).
* go-e MQTT (syssi/homeassistant-goecharger-mqtt 3.1.0): `select` `psm` (unique id `<serial>-select-psm-psm`,
  translation key `psm`, options `auto`, `one_phase`, `three_phases`); the `modelStatus` sensor's slug `phase_switch`.
* Fronius Wattpilot (mk-maddin/wattpilot-HA 0.4.1): `select` `psm` (unique id `<charger>-psm`, no translation key,
  options `Auto`, `1 Phase`, `3 Phases`), created only when the charger has the property; the `modelStatus` sensor
  (`<charger>-modelStatus`) says `NotChargingBecausePhaseSwitch`.

Each is found on the charger's own device through the entity registry; without it (absent, disabled, a single-phase
model) there is no capability.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution.chargers.base import (
    PHASES_INVALID,
    PHASES_TARGET_UNAVAILABLE,
    PHASES_UNSUPPORTED,
    PHASES_WRITE_FAILED,
    PhaseSwitchResult,
)
from custom_components.spotnav.flows.charger_detection import detect_charger

from ..charger_helpers import adapter_for, Clock
from ..charger_shapes import _e, E, register_shape, Shape, SHAPES

pytestmark = pytest.mark.usefixtures("offline_relay")

API2_OPTIONS = {"options": ["0", "1", "2"]}
MQTT_OPTIONS = {"options": ["auto", "one_phase", "three_phases"]}
WATTPILOT_OPTIONS = {"options": ["Auto", "1 Phase", "3 Phases"]}
PHASE_COUNT = {"min": 1, "max": 3, "step": 1, "mode": "slider"}


def _shape(platform: str, *extra: E) -> Shape:
    shape = SHAPES[platform]
    return replace(shape, entities=(*shape.entities, *extra))


def _lektrico(force: str = "off", reason: str = "no_limit") -> Shape:
    return _shape(
        "lektrico",
        _e("switch", "LK1_force_single_phase", "force_single_phase", force, translation_key=True),
        _e("sensor", "LK1_limit_reason", "limit_reason", reason, translation_key=True),
    )


def _nrgkick(count: str = "3.0", most: int = 3) -> Shape:
    return _shape(
        "nrgkick", _e("number", "NK42_phase_count", "phase_count", count, {**PHASE_COUNT, "max": most}, translation_key=True)
    )


def _goe_api2(psm: str = "0", status: str = "Charging because of forced state 'on'") -> Shape:
    return _shape(
        "goecharger_api2",
        _e("select", "goecharger_api2.goe_123456_psm", "psm", psm, API2_OPTIONS, translation_key=True),
        _e("sensor", "goecharger_api2.goe_123456_modelstatus_value", "modelstatus_value", status, translation_key=True),
        _e("sensor", "goecharger_api2.goe_123456_modelstatus", "modelstatus", "3", translation_key=True, disabled=True),
        # The read-only phase switch mode sensor of another key must not be taken for the select.
        _e("sensor", "goecharger_api2.goe_123456_psmd", "psmd", "10"),
    )


def _goe_mqtt(psm: str = "auto", status: str = "forced_on") -> Shape:
    return _shape(
        "goecharger_mqtt",
        _e("select", "654321-select-psm-psm", "psm", psm, MQTT_OPTIONS, translation_key=True),
        _e("sensor", "654321-sensor-modelStatus-modelStatus", "modelstatus", status, translation_key=True),
    )


def _wattpilot(psm: str = "Auto", status: str = "ChargingBecauseForceStateOn") -> Shape:
    return _shape(
        "wattpilot",
        _e("select", "WP1-psm", "psm", psm, WATTPILOT_OPTIONS),
        _e("sensor", "WP1-modelStatus", "modelStatus", status),
    )


#: Per platform: the shape with the control, its entity id, how 1 and 3 phases read, the service a set calls and what
#: it sends for 1 and 3, the status sensor and its switching value (`None`: the integration reports none).
CASES = {
    "lektrico": (
        _lektrico,
        "switch.lektrico_force_single_phase",
        ("on", "off"),
        [("switch", "turn_on", None), ("switch", "turn_off", None)],
        ("sensor.lektrico_limit_reason", "switching_phases"),
    ),
    "nrgkick": (
        _nrgkick,
        "number.nrgkick_phase_count",
        ("1.0", "3.0"),
        [("number", "set_value", {"value": 1}), ("number", "set_value", {"value": 3})],
        None,
    ),
    "goecharger_api2": (
        _goe_api2,
        "select.goecharger_api2_psm",
        ("1", "2"),
        [("select", "select_option", {"option": "1"}), ("select", "select_option", {"option": "2"})],
        ("sensor.goecharger_api2_modelstatus_value", "Not charging because of phase switch"),
    ),
    "goecharger_mqtt": (
        _goe_mqtt,
        "select.goecharger_mqtt_psm",
        ("one_phase", "three_phases"),
        [("select", "select_option", {"option": "one_phase"}), ("select", "select_option", {"option": "three_phases"})],
        ("sensor.goecharger_mqtt_modelstatus", "phase_switch"),
    ),
    "wattpilot": (
        _wattpilot,
        "select.wattpilot_psm",
        ("1 Phase", "3 Phases"),
        [("select", "select_option", {"option": "1 Phase"}), ("select", "select_option", {"option": "3 Phases"})],
        ("sensor.wattpilot_modelstatus", "NotChargingBecausePhaseSwitch"),
    ),
}
PLATFORMS = list(CASES)


def _adapter(hass: HomeAssistant, shape: Shape):
    ids = register_shape(hass, shape)
    return adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=Clock())


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_the_control_is_found_on_the_chargers_device(hass: HomeAssistant, platform: str) -> None:
    make, entity_id, *_ = CASES[platform]
    adapter = _adapter(hass, make())

    switch = adapter.phase_switch

    assert switch is not None and switch.entity_id == entity_id
    # A live switch faulted a car on an NRGkick and may leave a Wattpilot paused: their phases are set before a start.
    assert switch.start_only is (platform in ("nrgkick", "wattpilot"))


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_without_the_control_there_is_no_capability(hass: HomeAssistant, platform: str) -> None:
    adapter = _adapter(hass, SHAPES[platform])

    assert adapter.phase_switch is None
    assert await adapter.async_set_phases(1) == PhaseSwitchResult(False, PHASES_UNSUPPORTED)


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_a_disabled_control_is_no_capability(hass: HomeAssistant, platform: str) -> None:
    make, entity_id, *_ = CASES[platform]
    adapter = _adapter(hass, make())
    er.async_get(hass).async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER)

    assert adapter.phase_switch is None


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_it_is_found_by_what_it_is_not_by_its_name(hass: HomeAssistant, platform: str) -> None:
    make, entity_id, *_ = CASES[platform]
    adapter = _adapter(hass, make())
    renamed = f"{entity_id.split('.')[0]}.garage_phases"
    er.async_get(hass).async_update_entity(entity_id, new_entity_id=renamed)

    assert adapter.phase_switch is not None and adapter.phase_switch.entity_id == renamed


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_the_read_back_follows_the_control(hass: HomeAssistant, platform: str) -> None:
    make, entity_id, (one, three), *_ = CASES[platform]
    adapter = _adapter(hass, make())
    switch = adapter.phase_switch
    assert switch is not None
    attributes = dict(hass.states.get(entity_id).attributes)

    hass.states.async_set(entity_id, one, attributes)
    assert switch.phases_now() == 1
    hass.states.async_set(entity_id, three, attributes)
    assert switch.phases_now() == 3
    for unknown in ("unavailable", "unknown"):
        hass.states.async_set(entity_id, unknown, attributes)
        assert switch.phases_now() is None


@pytest.mark.parametrize(
    ("platform", "between"),
    [("nrgkick", "2.0"), ("goecharger_api2", "0"), ("goecharger_mqtt", "auto"), ("wattpilot", "Auto")],
)
async def test_auto_or_two_phases_is_not_a_phase_count_of_ours(hass: HomeAssistant, platform: str, between: str) -> None:
    make, entity_id, *_ = CASES[platform]
    adapter = _adapter(hass, make())
    hass.states.async_set(entity_id, between, dict(hass.states.get(entity_id).attributes))

    assert adapter.phase_switch.phases_now() is None


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_a_set_writes_one_or_three_phases(hass: HomeAssistant, platform: str) -> None:
    make, entity_id, _reads, expected, _status = CASES[platform]
    adapter = _adapter(hass, make())
    for domain, service in {(domain, service) for domain, service, _ in expected}:
        async_mock_service(hass, domain, service)

    assert await adapter.async_set_phases(1) == PhaseSwitchResult(True)
    assert await adapter.async_set_phases(3) == PhaseSwitchResult(True)

    sent = [
        (record["calls"][0]["service"], {k: v for k, v in record["calls"][0]["data"].items() if k != "entity_id"})
        for record in adapter.command_log()
    ]
    assert sent == [(f"{domain}.{service}", data or {}) for domain, service, data in expected]
    assert all(record["calls"][0]["data"]["entity_id"] == entity_id for record in adapter.command_log())
    assert [(record["kind"], record["result"]) for record in adapter.command_log()] == [
        ("phases", "sent"),
        ("phases", "sent"),
    ]


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_an_unavailable_control_is_not_executed(hass: HomeAssistant, platform: str) -> None:
    make, entity_id, _reads, expected, _status = CASES[platform]
    adapter = _adapter(hass, make())
    domain, service, _ = expected[0]
    calls = async_mock_service(hass, domain, service)
    hass.states.async_set(entity_id, "unavailable")

    assert await adapter.async_set_phases(1) == PhaseSwitchResult(False, PHASES_TARGET_UNAVAILABLE)
    assert calls == []


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_neither_one_nor_three_is_refused(hass: HomeAssistant, platform: str) -> None:
    make, *_ = CASES[platform]
    adapter = _adapter(hass, make())

    assert await adapter.async_set_phases(2) == PhaseSwitchResult(False, PHASES_INVALID)  # type: ignore[arg-type]


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_a_service_error_is_a_failed_switch_not_a_crash(hass: HomeAssistant, platform: str) -> None:
    make, _entity_id, _reads, expected, _status = CASES[platform]
    adapter = _adapter(hass, make())
    domain, service, _ = expected[0]

    async def refuse(call: ServiceCall) -> None:
        raise HomeAssistantError("the charger said no")

    hass.services.async_register(domain, service, refuse)

    assert await adapter.async_set_phases(1) == PhaseSwitchResult(False, PHASES_WRITE_FAILED)
    record = adapter.command_log()[-1]
    assert (record["kind"], record["result"]) == ("phases", "error")
    assert record["error"]["type"] == "HomeAssistantError"


@pytest.mark.parametrize("platform", [p for p in PLATFORMS if CASES[p][4] is not None])
async def test_the_chargers_own_switching_status_is_reported(hass: HomeAssistant, platform: str) -> None:
    make, _entity_id, _reads, _expected, (status_entity, switching) = CASES[platform]
    adapter = _adapter(hass, make())
    switch = adapter.phase_switch
    assert switch is not None

    assert switch.switching() is False
    hass.states.async_set(status_entity, switching)
    assert switch.switching() is True
    hass.states.async_set(status_entity, "unavailable")
    assert switch.switching() is False


async def test_nrgkick_reports_no_switching_status(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _nrgkick())

    assert adapter.phase_switch is not None and adapter.phase_switch.switching() is False


async def test_go_e_api2_reads_the_raw_code_where_it_is_enabled_and_the_german_words(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _goe_api2())
    er.async_get(hass).async_update_entity("sensor.goecharger_api2_modelstatus", disabled_by=None)
    switch = adapter.phase_switch
    assert switch is not None

    hass.states.async_set("sensor.goecharger_api2_modelstatus", "23")
    assert switch.switching() is True
    hass.states.async_set("sensor.goecharger_api2_modelstatus", "3")
    hass.states.async_set("sensor.goecharger_api2_modelstatus_value", "Nicht laden während der Phasenumschaltung")
    assert switch.switching() is True


async def test_go_e_never_writes_auto(hass: HomeAssistant) -> None:
    for platform in ("goecharger_api2", "goecharger_mqtt"):
        make, entity_id, _reads, _expected, _status = CASES[platform]
        adapter = _adapter(hass, make())
        calls = async_mock_service(hass, "select", "select_option")

        await adapter.async_set_phases(1)
        await adapter.async_set_phases(3)

        assert all(call.data["option"] not in ("0", "auto") for call in calls)
        assert [call.data["entity_id"] for call in calls] == [entity_id, entity_id]
        hass.services.async_remove("select", "select_option")


async def test_a_select_without_a_known_option_is_not_executed(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _wattpilot())
    calls = async_mock_service(hass, "select", "select_option")
    hass.states.async_set("select.wattpilot_psm", "Auto", {"options": ["Auto", "Eins", "Drei"]})

    assert await adapter.async_set_phases(1) == PhaseSwitchResult(False, PHASES_TARGET_UNAVAILABLE)
    assert calls == []


async def test_lektrico_on_a_single_phase_model_has_no_switch(hass: HomeAssistant) -> None:
    # The integration creates the force-single-phase switch only on the three-phase model; the limit reason exists
    # on every model.
    shape = _shape("lektrico", _e("sensor", "LK1_limit_reason", "limit_reason", "no_limit", translation_key=True))
    adapter = _adapter(hass, shape)

    assert adapter.phase_switch is None


async def test_nrgkick_with_a_single_phase_connector_has_no_switch(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _nrgkick(count="1.0", most=1))

    assert adapter.phase_switch is None


async def test_nrgkick_with_a_three_phase_connector_attached_later_has_one(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _nrgkick(count="1.0", most=1))
    hass.states.async_set("number.nrgkick_phase_count", "1.0", {**PHASE_COUNT, "max": 3})

    assert adapter.phase_switch is not None


async def test_wattpilot_without_the_property_has_no_select(hass: HomeAssistant) -> None:
    # The integration does not create the select when the charger lacks the `psm` property; the status sensor alone
    # is no capability.
    shape = _shape("wattpilot", _e("sensor", "WP1-modelStatus", "modelStatus", "ChargingBecauseForceStateOn"))
    adapter = _adapter(hass, shape)

    assert adapter.phase_switch is None


async def test_a_psm_sensor_is_not_the_select(hass: HomeAssistant) -> None:
    # go-e MQTT also has a read-only `psm` sensor (`<serial>-sensor-psm-0`, translation key `psm`).
    shape = _shape("goecharger_mqtt", _e("sensor", "654321-sensor-psm-0", "psm", "auto", translation_key=True))
    adapter = _adapter(hass, shape)

    assert adapter.phase_switch is None
