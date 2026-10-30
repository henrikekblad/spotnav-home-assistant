"""Zendure, Anker Solix, EcoFlow and Marstek home batteries in the site detection table, from the entity
registry entries each integration creates: which entity is the battery's power, and which way it counts
(SpotNav reads charging as positive). Batteries whose sign is not known are not detected.
"""

from __future__ import annotations

from .test_site_detection import Registry

# ---- Zendure --------------------------------------------------------------------------------------


def zendure(*devices: str) -> Registry:
    """`zendure_ha`: a manager device and one device per SolarFlow/Hyper; unique ids are
    `snakecase(<device name lower>_<property>)` and translation keys `snakecase(<property>)`."""
    r = Registry()
    r.device("manager", name="Zendure Manager", entry="zen")
    r.add("zendure_ha", "zendure_manager_power", "zendure_manager_power", device_class="power", unit="W", device="manager", entry="zen", translation_key="power")
    for name in devices:
        slug = name.lower().replace(" ", "_")
        r.device(slug, model=name, manufacturer="Zendure", name=name, entry="zen")
        for key in (
            "grid_input_power",
            "solar_input_power",
            "output_pack_power",
            "pack_input_power",
            "output_home_power",
            "bat_in_out",
        ):
            r.add("zendure_ha", f"{slug}_{key}", f"{slug}_{key}", device_class="power", unit="W", device=slug, entry="zen", translation_key=key)
    return r


def test_the_zendure_battery_is_the_pack_charge_and_discharge_pair() -> None:
    [battery] = zendure("Hyper 2000").detect().batteries

    assert battery.integration == "zendure_ha"
    assert battery.entity_id == "sensor.hyper_2000_output_pack_power"
    assert battery.discharge_entity_id == "sensor.hyper_2000_pack_input_power"
    assert battery.inverted is False


def test_the_zendure_manager_power_is_a_setpoint_and_not_the_battery() -> None:
    assert zendure().detect().batteries == ()


# ---- Anker Solix ----------------------------------------------------------------------------------


def anker(*, total: bool = True, solarbanks: tuple[str, ...] = ("APC1",), pps: bool = False) -> Registry:
    """`anker_solix`: unique ids are `<context>_<key>`, the context the system's site id or the device's
    serial; the keys are also the translation keys."""
    r = Registry()
    r.device("system", model="Solarbank System", manufacturer="Anker", name="Home", entry="anker")

    def add(device: str, context: str, key: str) -> None:
        r.add("anker_solix", f"{context.lower()}_{key}", f"{context}_{key}".lower(), device_class="power", unit="W", device=device, entry="anker", translation_key=key)

    if total:
        add("system", "site1", "solarbank_battery_power_signed")
    if pps:
        add("system", "site1", "pps_battery_power_signed")
    for serial in solarbanks:
        r.device(serial, model="A17C1", manufacturer="Anker", name=f"Solarbank {serial}", entry="anker")
        for key in ("battery_power_signed", "bat_charge_power", "bat_discharge_power", "photovoltaic_power"):
            add(serial, serial, key)
    return r


def test_the_anker_system_total_is_read_first_and_is_charge_positive() -> None:
    [battery] = anker(solarbanks=("APC1", "APC2")).detect().batteries

    assert battery.integration == "anker_solix"
    assert battery.entity_id == "sensor.site1_solarbank_battery_power_signed"
    assert battery.inverted is False and battery.discharge_entity_id is None


def test_an_anker_device_without_a_system_total_is_read_by_its_signed_battery_power() -> None:
    [battery] = anker(total=False).detect().batteries

    assert battery.entity_id == "sensor.apc1_battery_power_signed"
    assert battery.inverted is False


def test_the_anker_power_panel_total_is_not_taken_for_a_battery() -> None:
    assert anker(total=False, solarbanks=(), pps=True).detect().batteries == ()


# ---- EcoFlow --------------------------------------------------------------------------------------


def ecoflow_powerstream(*, public: bool = False) -> Registry:
    """`ecoflow_cloud`: unique ids are `ecoflow-[api-]<sn>-<key>` with `.` and `_` turned into `-`."""
    r = Registry()
    r.device("ps", model="PowerStream", manufacturer="EcoFlow", name="PowerStream", entry="eco")
    prefix = "ecoflow-api-HW51ZEH4SF5U0000" if public else "ecoflow-HW51ZEH4SF5U0000"
    for key, name in (
        ("20-1-pv1InputWatts", "Solar 1 Watts"),
        ("20-1-batInputWatts", "Battery Input Watts"),
        ("20-1-batInputVolt", "Battery Input Potential"),
        ("20-1-invOutputWatts", "Inverter Output Watts"),
    ):
        r.add("ecoflow_cloud", f"powerstream_{name.lower().replace(' ', '_')}", f"{prefix}-{key}", device_class="power", unit="W", device="ps", entry="eco", original_name=name)
    return r


def test_the_powerstream_battery_input_is_discharge_positive_and_is_negated() -> None:
    for public in (False, True):
        [battery] = ecoflow_powerstream(public=public).detect().batteries

        assert battery.integration == "ecoflow_cloud"
        assert battery.entity_id == "sensor.powerstream_battery_input_watts"
        assert battery.inverted is True


def test_the_ocean_pro_battery_power_is_charge_positive() -> None:
    r = Registry()
    r.device("op", model="Ocean Pro", manufacturer="EcoFlow", name="Ocean Pro", entry="eco")
    r.add("ecoflow_cloud", "ocean_pro_grid_power", "ecoflow-R371ZD1AZH2X0000-ocean-grid-pwr", device_class="power", unit="W", device="op", entry="eco", original_name="Grid Power")
    r.add("ecoflow_cloud", "ocean_pro_battery_power", "ecoflow-R371ZD1AZH2X0000-ocean-batt-pwr", device_class="power", unit="W", device="op", entry="eco", original_name="Battery Power")

    [battery] = r.detect().batteries

    assert battery.entity_id == "sensor.ocean_pro_battery_power" and battery.inverted is False


def test_ecoflow_batteries_whose_sign_is_not_known_are_not_detected() -> None:
    r = Registry()
    r.device("po", model="PowerOcean", manufacturer="EcoFlow", name="PowerOcean", entry="eco")
    r.add("ecoflow_cloud", "powerocean_bppwr", "ecoflow-api-HJ31ZDH4ZF6W0000-bpPwr", device_class="power", unit="W", device="po", entry="eco", original_name="bpPwr")
    r.add("ecoflow_cloud", "powerocean_bp1pwr", "ecoflow-api-HJ31ZDH4ZF6W0000-bp1-bpPwr", device_class="power", unit="W", device="po", entry="eco", original_name="bp1Pwr")
    r.device("st", model="Stream Ultra", manufacturer="EcoFlow", name="Stream Ultra", entry="eco")
    r.add("ecoflow_cloud", "stream_battery_power", "ecoflow-BK11ZEB4HF3J0000-powGetBpCms", device_class="power", unit="W", device="st", entry="eco", original_name="Battery Power")

    assert r.detect().batteries == ()


# ---- Marstek --------------------------------------------------------------------------------------


def marstek(*macs: str, system: bool = True) -> Registry:
    """`marstek_local_api`: `<mac>_<key>` per device and `<macs joined>_<key>` on the system device."""
    r = Registry()
    for mac in macs:
        r.device(mac, model="VenusE", manufacturer="Marstek", name=f"Venus {mac[-4:]}", entry="marstek")
        for key in ("battery_power", "battery_power_in", "battery_power_out", "grid_power", "ct_total_power"):
            r.add("marstek_local_api", f"venus_{mac[-4:]}_{key}", f"{mac}_{key}", device_class="power", unit="W", device=mac, entry="marstek")
    if system:
        joined = "_".join(sorted(macs))
        r.device("system", model="Marstek System", manufacturer="Marstek", name="Marstek System", entry="marstek")
        for key in ("system_total_power", "system_total_power_in", "system_total_power_out", "system_total_grid_power"):
            r.add("marstek_local_api", f"marstek_system_{key}", f"{joined}_{key}", device_class="power", unit="W", device="system", entry="marstek")
    return r


def test_the_marstek_system_total_is_read_first_and_is_charge_positive() -> None:
    [battery] = marstek("aabbccddee01", "aabbccddee02").detect().batteries

    assert battery.integration == "marstek_local_api"
    assert battery.entity_id == "sensor.marstek_system_system_total_power"
    assert battery.inverted is False and battery.discharge_entity_id is None


def test_a_single_marstek_is_read_by_its_own_battery_power() -> None:
    [battery] = marstek("aabbccddee01", system=False).detect().batteries

    assert battery.entity_id == "sensor.venus_ee01_battery_power" and battery.inverted is False


def test_marstek_over_modbus_is_not_detected_because_its_sign_is_not_known() -> None:
    r = Registry()
    r.device("venus", model="Venus E", manufacturer="Marstek", name="Venus", entry="mm")
    r.add("marstek_modbus", "venus_battery_power", "01JABCDEF_battery_power", device_class="power", unit="W", device="venus", entry="mm")

    assert r.detect().batteries == ()
