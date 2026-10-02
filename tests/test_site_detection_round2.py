"""Sungrow, SolaX and Perific in the site detection table, from the entity shapes their integrations
register (second charger audit): which entities make a grid meter, which way the power and the battery
count, and which devices balance load by themselves.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.const import MEASUREMENT_MODE_DERIVED
from custom_components.spotnav.execution.chargers.base import ASSIGN_EXTERNAL_BALANCER, WRITE_SESSION_START
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.site.site_detection import (
    BATTERY_ROWS,
    find_own_load_balancing,
    freshness_warnings,
    RegistryDevice,
    UPDATE_BEHAVIOUR,
    WARNING_SIGN_UNVERIFIED,
)

from .charger_helpers import adapter_for
from .charger_shapes import register_shape, SHAPES
from .test_site_detection import one_meter, Registry, roles


# ---- Sungrow --------------------------------------------------------------------------------------


def sungrow_kroperuk_local() -> Registry:
    """KRoperUK's integration on local Modbus: power, voltage and an unsigned current per phase."""
    r = Registry()
    r.device("sg_meter", model="Smart Meter", manufacturer="Sungrow", name="Smart Meter", entry="sg")
    for ph in "abc":
        r.add(
            "sungrow",
            f"smart_meter_meter_phase_{ph}_active_power",
            f"1234_uuid1_meter_phase_{ph}_active_power",
            device_class="power",
            unit="W",
            device="sg_meter",
            entry="sg",
            original_name=f"Meter Phase {ph.upper()} Active Power",
        )
        r.add(
            "sungrow",
            f"smart_meter_meter_phase_{ph}_voltage",
            f"1234_uuid1_meter_phase_{ph}_voltage",
            device_class="voltage",
            unit="V",
            device="sg_meter",
            entry="sg",
            original_name=f"Meter Phase {ph.upper()} Voltage",
        )
        r.add(
            "sungrow",
            f"smart_meter_meter_phase_{ph}_current",
            f"1234_uuid1_meter_phase_{ph}_current",
            device_class="current",
            unit="A",
            device="sg_meter",
            entry="sg",
            original_name=f"Meter Phase {ph.upper()} Current",
        )
    return r


def sungrow_modbus_package(*suffixes: str) -> Registry:
    """mkaiser's YAML package: plain `modbus` sensors, no device, `sg_` unique ids."""
    r = Registry()
    for suffix in suffixes or ("",):
        for ph in "abc":
            for kind, device_class, unit in (
                ("active_power", "power", "W"),
                ("voltage", "voltage", "V"),
                ("current", "current", "A"),
            ):
                r.add(
                    "modbus",
                    f"meter_phase_{ph}_{kind}{suffix}",
                    f"sg_meter_phase_{ph}_{kind}{suffix}",
                    device_class=device_class,
                    unit=unit,
                    device=None,
                    entry="",
                )
    return r


def test_the_kroperuk_meter_is_derived_from_import_positive_power_with_an_unsigned_current() -> None:
    meter = one_meter(sungrow_kroperuk_local())

    assert meter.integration == "sungrow" and meter.mode == MEASUREMENT_MODE_DERIVED
    assert meter.power_inverted is False and meter.signed_current is False
    assert meter.estimated is False
    assert set(roles(meter)) == {"power", "voltage", "current"}
    assert meter.derived_entities["L2"]["power"] == "sensor.smart_meter_meter_phase_b_active_power"


def test_the_sungrow_modbus_package_is_a_meter_without_a_device() -> None:
    meter = one_meter(sungrow_modbus_package())

    assert meter.integration == "modbus" and meter.power_inverted is False
    assert meter.derived_entities["L1"]["power"] == "sensor.meter_phase_a_active_power"
    assert meter.derived_entities["L3"]["voltage"] == "sensor.meter_phase_c_voltage"


def test_a_second_inverters_modbus_set_is_its_own_meter() -> None:
    detection = sungrow_modbus_package("", "_inv_2").detect()

    assert sorted(m.candidate_id for m in detection.meters) == ["modbus", "modbus:inv_2"]


def test_other_modbus_sensors_with_the_same_names_are_not_taken_for_sungrow() -> None:
    r = Registry()
    for ph in "abc":
        r.add("modbus", f"meter_phase_{ph}_active_power", f"other_meter_phase_{ph}_active_power", device_class="power", unit="W", device=None, entry="")
        r.add("modbus", f"meter_phase_{ph}_voltage", f"other_meter_phase_{ph}_voltage", device_class="voltage", unit="V", device=None, entry="")

    # The generic search may still offer them as an unknown meter; the Sungrow row does not claim them.
    assert [m for m in r.detect().meters if not m.candidate_id.startswith("generic")] == []


def test_the_sungrow_phase_inverter_registers_are_not_the_grid_meter() -> None:
    r = Registry()
    for ph in "abc":
        r.add("modbus", f"phase_{ph}_voltage", f"sg_phase_{ph}_voltage", device_class="voltage", unit="V", device=None, entry="")
        r.add("modbus", f"phase_{ph}_current", f"sg_phase_{ph}_current", device_class="current", unit="A", device=None, entry="")

    assert [m for m in r.detect().meters if not m.candidate_id.startswith("generic")] == []


def sungrow_battery_registry(*, local: bool, cloud: bool) -> Registry:
    r = Registry()
    if local:
        r.add("sungrow", "inverter_battery_power", "1234_uuid1_battery_power", device_class="power", unit="W", entry="sg", original_name="Battery Power")
    if cloud:
        r.add(
            "sungrow",
            "plant_battery_power",
            "1234_total_field_energy_storage_active_power",
            device_class="power",
            unit="W",
            entry="sgc",
            original_name="Battery Power",
        )
    return r


def test_the_local_sungrow_battery_is_discharge_positive_and_is_negated() -> None:
    [battery] = sungrow_battery_registry(local=True, cloud=False).detect().batteries

    assert battery.entity_id == "sensor.inverter_battery_power" and battery.inverted is True


def test_the_cloud_battery_is_charge_positive_and_is_taken_by_its_code_not_its_name() -> None:
    [battery] = sungrow_battery_registry(local=False, cloud=True).detect().batteries

    assert battery.entity_id == "sensor.plant_battery_power" and battery.inverted is False


def test_a_cloud_and_a_local_entry_each_get_their_own_sign() -> None:
    batteries = sungrow_battery_registry(local=True, cloud=True).detect().batteries

    assert {b.entity_id: b.inverted for b in batteries} == {
        "sensor.inverter_battery_power": True,
        "sensor.plant_battery_power": False,
    }


def test_the_cloud_ess_pair_is_charge_and_discharge_power() -> None:
    r = Registry()
    r.add("sungrow", "ess_charge", "1234_ess_battery_charge_power", device_class="power", unit="W", entry="sgc")
    r.add("sungrow", "ess_discharge", "1234_ess_battery_discharge_power", device_class="power", unit="W", entry="sgc")

    [battery] = r.detect().batteries

    assert battery.entity_id == "sensor.ess_charge" and battery.discharge_entity_id == "sensor.ess_discharge"
    assert battery.inverted is False


def test_the_modbus_package_battery_is_negated_and_only_its_own_id_counts() -> None:
    r = Registry()
    r.add("modbus", "battery_power", "sg_battery_power", device_class="power", unit="W", device=None, entry="")
    r.add("modbus", "someone_battery_power", "other_battery_power", device_class="power", unit="W", device=None, entry="")

    [battery] = r.detect().batteries

    assert battery.entity_id == "sensor.battery_power" and battery.inverted is True


def test_there_is_no_sungrow_sungrow_integration() -> None:
    platforms = {platform for row in BATTERY_ROWS for platform in row.platforms}

    assert "sungrow_sungrow" not in platforms
    # Its cloud and local transports poll at different rates and nothing tells the two apart.
    assert "sungrow" not in UPDATE_BEHAVIOUR
    assert freshness_warnings({"sensor.smart_meter_meter_phase_a_active_power": "sungrow"}, 5.0) == []


# ---- SolaX ----------------------------------------------------------------------------------------


def solax_hybrid(*, manufacturer: str = "SolaX Power", meter_2: bool = False) -> Registry:
    """The SolaX plugin of `solax_modbus` on a three-phase hybrid: unique ids `<name>_<key>`."""
    r = Registry()
    r.device("solax_inv", model="X3-Hybrid-G4", manufacturer=manufacturer, name="SolaX Inverter", entry="solax")
    for n in "123":
        r.add(
            "solax_modbus",
            f"solax_measured_power_l{n}",
            f"SolaX_measured_power_l{n}",
            device_class="power",
            unit="W",
            device="solax_inv",
            entry="solax",
            original_name=f"Measured Power L{n}",
        )
        r.add(
            "solax_modbus",
            f"solax_grid_voltage_l{n}",
            f"SolaX_grid_voltage_l{n}",
            device_class="voltage",
            unit="V",
            device="solax_inv",
            entry="solax",
            disabled=True,
            original_name=f"Grid Voltage L{n}",
        )
        if meter_2:
            r.add(
                "solax_modbus",
                f"solax_meter_2_measured_power_l{n}",
                f"SolaX_meter_2_measured_power_l{n}",
                device_class="power",
                unit="W",
                device="solax_inv",
                entry="solax",
                original_name=f"Meter 2 Measured Power L{n}",
            )
    return r


def test_the_solax_meter_is_export_positive_so_its_power_is_negated_and_the_sign_is_flagged() -> None:
    meter = one_meter(solax_hybrid())

    assert meter.power_inverted is True and meter.mode == MEASUREMENT_MODE_DERIVED
    assert WARNING_SIGN_UNVERIFIED in meter.warnings
    assert meter.derived_entities["L1"]["power"] == "sensor.solax_measured_power_l1"
    assert meter.derived_entities["L1"]["voltage"] == "sensor.solax_grid_voltage_l1"
    # The voltage ships disabled: it is offered to be enabled, never assumed on.
    assert len(meter.disabled_entity_ids) == 3


def test_the_solax_second_meter_is_left_out() -> None:
    meter = one_meter(solax_hybrid(meter_2=True))

    assert all("meter_2" not in entity_id for entity_id in meter.derived_entities["L2"].values())


def test_another_plugin_of_the_integration_is_not_assumed_to_share_solax_conventions() -> None:
    assert solax_hybrid(manufacturer="Solinteg").detect().meters == ()


# ---- Perific --------------------------------------------------------------------------------------


def test_a_perific_device_of_any_model_is_a_device_that_balances_load_by_itself() -> None:
    devices = [
        RegistryDevice("p1", model="EM2One", name="Perific One", config_entry_ids=("perific_entry",)),
        RegistryDevice("p2", model="Monitor", name="Enegic Monitor", config_entry_ids=("perific_entry",)),
        RegistryDevice("p3", model=None, name="Meter", config_entry_ids=("perific_entry",)),
    ]

    found = find_own_load_balancing(devices, {"perific_entry": "perific"})

    assert [(item.integration, item.device_name) for item in found] == [
        ("perific", "Perific One"),
        ("perific", "Enegic Monitor"),
        ("perific", "Meter"),
    ]


async def test_a_zaptec_charger_beside_perific_is_started_and_stopped_only(hass: HomeAssistant) -> None:
    perific = MockConfigEntry(domain="perific", entry_id="perific")
    perific.add_to_hass(hass)
    ids = register_shape(hass, SHAPES["zaptec"])

    found = detect_charger(hass, ids["device_id"])

    assert found.charge_control == "switch.zaptec_charger_operation_mode" and found.control_path is not None
    assert found.current_limit is None and found.current_control == ""
    assert found.balanced_by == "perific" and "external_balancer" in found.notes


async def test_a_zaptec_current_already_configured_is_refused_while_perific_is_there(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["zaptec"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    assert adapter.capabilities.set_current is True

    MockConfigEntry(domain="perific", entry_id="perific").add_to_hass(hass)

    assert adapter.capabilities.set_current is False and adapter.capabilities.regulated_current is False
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_EXTERNAL_BALANCER


async def test_without_perific_zaptec_keeps_its_current(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["zaptec"])

    found = detect_charger(hass, ids["device_id"])

    assert found.current_limit == "number.zaptec_available_current" and found.balanced_by is None


async def test_the_site_warns_about_perific_beside_a_zaptec_charger(hass: HomeAssistant) -> None:
    from custom_components.spotnav.api.entity_fields import _external_balancer_warnings
    from custom_components.spotnav.const import CONF_CHARGER_PLATFORM, DOMAIN

    assert _external_balancer_warnings(hass) == []
    MockConfigEntry(domain="perific", entry_id="perific").add_to_hass(hass)
    assert _external_balancer_warnings(hass) == []  # no SpotNav charger of that platform yet
    MockConfigEntry(domain=DOMAIN, entry_id="charger", data={CONF_CHARGER_PLATFORM: "zaptec"}).add_to_hass(hass)

    [warning] = _external_balancer_warnings(hass)

    assert warning["code"] == "external_current_balancer" and warning["integration"] == "perific"
    assert warning["device_name"] == "Zaptec"

