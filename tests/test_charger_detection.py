"""Detection of a charger's controls from its device, per integration, from recorded entity shapes.

The device is found first, then its own integration's entities by platform and key (translation key
for core integrations, the tail of the unique id for custom ones), never by entity id or name.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.execution.charger_profiles import (
    PROFILES,
    ROLE_CHARGER,
    ROLE_EXCLUDED,
    ROLE_EXTERNAL_CONTROLLER,
    ROLE_MEASUREMENT_ONLY,
    ROLE_UNSUPPORTED,
)

from .charger_shapes import E, ENERGY_KWH, register_shape, Shape, SHAPES


@pytest.mark.parametrize("platform", sorted(SHAPES))
async def test_each_integrations_controls_are_found_from_its_device(
    hass: HomeAssistant, platform: str
) -> None:
    shape = SHAPES[platform]
    ids = register_shape(hass, shape)

    found = detect_charger(hass, ids["device_id"])

    assert found is not None and found.platform == platform and found.role == ROLE_CHARGER
    expect = shape.expect
    assert found.charge_control == expect["charge_control"]
    if expect["path"] is None:
        assert found.control_path is None
    else:
        assert found.control_path is not None
        for key, value in expect["path"].items():
            assert found.control_path[key] == value
    assert found.current_limit == expect["current_limit"]
    assert found.current_control == expect["current_control"]
    assert found.energy_register == expect["energy_register"]
    assert found.session_energy_register == expect["session_energy_register"]
    assert (found.charging_state or {}).get("entity_id") == expect["charging_state"]
    assert found.current_entities == expect["current_entities"]
    assert sorted(found.disabled_useful) == sorted(expect.get("disabled_useful", []))
    assert [conflict.entity_id for conflict in found.conflicts] == expect.get("conflicts", [])
    assert found.external_controller is False


async def test_the_charging_values_come_with_the_status_sensor(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["monta"])

    found = detect_charger(hass, ids["device_id"])

    assert found.charging_state == {
        "entity_id": "sensor.monta_charger_state",
        "charging_values": ["busy-charging"],
    }


async def test_an_energy_register_is_the_lifetime_one_and_a_session_one_only_when_none_exists(
    hass: HomeAssistant,
) -> None:
    """Peblar has both: the lifetime `energy_total` wins and the session counter is not offered."""
    ids = register_shape(hass, SHAPES["peblar"])

    found = detect_charger(hass, ids["device_id"])

    assert found.energy_register == "sensor.peblar_energy_total"
    assert found.session_energy_register is None


async def test_a_milliampere_current_is_accepted_and_disabled_ones_are_reported(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["peblar"])
    registry = er.async_get(hass)
    assert registry.async_get("sensor.peblar_current_phase_1").disabled_by is not None

    found = detect_charger(hass, ids["device_id"])

    assert found.current_entities == [
        "sensor.peblar_current_phase_1",
        "sensor.peblar_current_phase_2",
        "sensor.peblar_current_phase_3",
    ]
    assert "sensor.peblar_current_phase_1" in found.disabled_useful


async def test_a_select_whose_options_name_no_start_is_not_guessed(hass: HomeAssistant) -> None:
    shape = Shape(
        "myenergi",
        "Z2",
        (E("select", "ENTRY-Z2-charge_mode", "charge_mode", "Red", {"options": ["Red", "Green", "Blue"]}),),
        {},
    )
    ids = register_shape(hass, shape)

    found = detect_charger(hass, ids["device_id"])

    assert found.control_path is None and found.charge_control is None
    assert "select_options_not_recognised" in found.notes


async def test_another_integrations_entities_on_the_same_device_are_ignored(hass: HomeAssistant) -> None:
    """A charger on both OCPP and its vendor cloud: only the vendor's own entities are read."""
    ids = register_shape(hass, SHAPES["wallbox"])
    other = MockConfigEntry(domain="ocpp", entry_id="ocpp_twin")
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    twin = registry.async_get_or_create(
        "switch",
        "ocpp",
        "switch.ocpp.cp.charge_control",
        suggested_object_id="cp_charge_control",
        config_entry=other,
        device_id=ids["device_id"],
    )
    hass.states.async_set(twin.entity_id, "on")

    found = detect_charger(hass, ids["device_id"])

    assert found.charge_control == "switch.wallbox_pause_resume"


async def test_the_chargers_own_mode_is_reported_while_it_is_on(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["goecharger_api2"])
    assert detect_charger(hass, ids["device_id"]).conflicts == []

    hass.states.async_set("select.goecharger_api2_lmo", "4", {"options": ["3", "4", "5"]})
    hass.states.async_set("switch.goecharger_api2_fup", "on")

    found = detect_charger(hass, ids["device_id"])

    assert {(c.entity_id, c.label) for c in found.conflicts} == {
        ("select.goecharger_api2_lmo", "charging mode"),
        ("switch.goecharger_api2_fup", "PV surplus"),
    }


async def test_an_unreadable_own_mode_is_not_a_conflict(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["openevse"])
    hass.states.async_set("switch.openevse_solar_pv_divert", "unavailable")

    assert detect_charger(hass, ids["device_id"]).conflicts == []


async def test_evcc_or_openwb_installed_means_warn_but_still_detect(
    hass: HomeAssistant,
) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    MockConfigEntry(domain="evcc_intg", entry_id="evcc").add_to_hass(hass)

    found = detect_charger(hass, ids["device_id"])

    # Installed elsewhere is a warning, not a reason to suggest nothing: the device is not evcc's own.
    assert found.external_installed is True
    assert found.external_controller is False
    assert found.charge_control is not None


@pytest.mark.parametrize(
    ("platform", "role"),
    [
        ("evcc_intg", ROLE_EXTERNAL_CONTROLLER),
        ("openwb2mqtt", ROLE_EXTERNAL_CONTROLLER),
        ("tesla_wall_connector", ROLE_MEASUREMENT_ONLY),
        ("andersen_ev", ROLE_UNSUPPORTED),
        ("webastoconnect", ROLE_EXCLUDED),
        ("webel_gctrl", ROLE_EXCLUDED),
        ("juicenet", ROLE_EXCLUDED),
        ("smappee", ROLE_EXCLUDED),
    ],
)
async def test_what_is_not_a_chargeable_charger_is_classified_not_detected(
    hass: HomeAssistant, platform: str, role: str
) -> None:
    ids = register_shape(hass, Shape(platform, "X1", (E("sensor", "X1_energy_kwh", "energy_kwh", "1", ENERGY_KWH),), {}))

    found = detect_charger(hass, ids["device_id"])

    assert found.role == role
    assert found.charge_control is None and found.control_path is None
    assert found.external_controller is (role == ROLE_EXTERNAL_CONTROLLER)


async def test_a_platform_nobody_described_is_not_guessed(hass: HomeAssistant) -> None:
    ids = register_shape(hass, Shape("acme_charger", "A1", (E("switch", "A1_charging", "charging", "on"),), {}))

    found = detect_charger(hass, ids["device_id"])

    assert found.role is None and found.charge_control is None and "platform_not_known" in found.notes


def test_every_profile_key_is_lower_case_and_every_charger_has_a_policy() -> None:
    for profile in PROFILES.values():
        assert profile.policy is not None
        for keys in (profile.current_keys, profile.energy_keys, profile.status_keys, profile.charging_values):
            assert all(key == key.lower() for key in keys), profile.platform


async def test_a_zaptec_installation_with_two_chargers_offers_no_current(hass: HomeAssistant) -> None:
    """The limit caps every charger under the installation, so it is not offered for one of two."""
    shape = SHAPES["zaptec"]
    first = register_shape(hass, shape)
    # A second charger on the same installation device.
    from homeassistant.helpers import device_registry as dr

    devices = dr.async_get(hass)
    config_entry = hass.config_entries.async_get_entry("zaptec")
    second_device = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("zaptec", "ZAP2")},
        name="zaptec second",
        via_device_id=first["installation_device_id"],
    )
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "switch",
        "zaptec",
        "ZAP2_charger_operation_mode",
        suggested_object_id="zaptec2_charger_operation_mode",
        config_entry=config_entry,
        device_id=second_device.id,
    )

    found = detect_charger(hass, first["device_id"])

    assert found.current_limit is None and found.current_control == ""
    assert "installation_shared" in found.notes
    # The charge control still works: it is this charger's own.
    assert found.charge_control == "switch.zaptec_charger_operation_mode"
