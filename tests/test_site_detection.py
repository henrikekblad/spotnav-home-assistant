"""The per-integration detection table (`site/site_detection.py`), driven by recorded-shape entity
registries: for each supported integration the registry entries it creates (platform, unique_id,
translation key, device, disabled-by-default state) go in, and the detected mode, entities, sign
fixes and estimated flag come out.

The shapes follow each integration's own entity model; nothing here talks to a live integration.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest

from custom_components.spotnav.const import MEASUREMENT_MODE_DERIVED, MEASUREMENT_MODE_DIRECT
from custom_components.spotnav.site.measurement_source import GridPowerSource
from custom_components.spotnav.site.site_detection import (
    apply_battery_candidate,
    apply_meter_candidate,
    detect_site,
    find_own_load_balancing,
    freshness_warnings,
    RegistryDevice,
    RegistryEntity,
    WARNING_OWN_LOAD_BALANCING,
    WARNING_MAY_MEASURE_SUBCIRCUIT,
    WARNING_ON_CHANGE_ONLY,
    WARNING_SIGN_UNVERIFIED,
    WARNING_SLOW_UPDATE,
    WARNING_VOLTAGE_OTHER_DEVICE,
)

PHASES = ("L1", "L2", "L3")
INTEGRATION = "integration"


@dataclass
class Registry:
    """A recorded registry: sensor entities and devices of one or more config entries."""

    entities: list[RegistryEntity] = field(default_factory=list)
    devices: list[RegistryDevice] = field(default_factory=list)

    def device(self, device_id: str, *, model: str | None = None, manufacturer: str | None = None, name: str | None = None, entry: str = "entry") -> str:
        self.devices.append(
            RegistryDevice(device_id, manufacturer=manufacturer, model=model, name=name or device_id, config_entry_ids=(entry,))
        )
        return device_id

    def add(
        self,
        platform: str,
        object_id: str,
        unique_id: str,
        *,
        device_class: str | None = None,
        unit: str | None = None,
        device: str | None = "dev",
        entry: str = "entry",
        disabled: bool = False,
        user_disabled: bool = False,
        translation_key: str | None = None,
        original_name: str | None = None,
    ) -> None:
        self.entities.append(
            RegistryEntity(
                entity_id=f"sensor.{object_id}",
                platform=platform,
                unique_id=unique_id,
                translation_key=translation_key,
                original_name=original_name,
                device_class=device_class,
                unit=unit,
                device_id=device,
                config_entry_id=entry,
                disabled_by=INTEGRATION if disabled else ("user" if user_disabled else None),
            )
        )

    def detect(self, **kwargs):
        return detect_site(self.entities, self.devices, **kwargs)


def one_meter(registry: Registry, **kwargs):
    detection = registry.detect(**kwargs)
    assert len(detection.meters) == 1, [m.candidate_id for m in detection.meters]
    return detection.meters[0]


def roles(candidate) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for item in candidate.entities:
        found.setdefault(item.role, set()).add(item.entity_id)
    return found


def suffixes(prefix: str, template: str, letters=("1", "2", "3")) -> list[str]:
    return [template.format(prefix=prefix, n=n) for n in letters]


# ---- recorded shapes ----------------------------------------------------------------------------


def shelly_pro_3em() -> Registry:
    """Shelly Pro 3EM, Gen2 triphase profile: current and voltage ship disabled."""
    r = Registry()
    mac = "aabbccddeeff"
    for ph in "abc":
        r.device(f"shelly_{ph}", model="Phase " + ph.upper(), name=f"Pro 3EM Phase {ph.upper()}", entry="shelly")
        r.add("shelly", f"pro3em_phase_{ph}_current", f"{mac}-em:0-{ph}_current", device_class="current", unit="A", device=f"shelly_{ph}", entry="shelly", disabled=True)
        r.add("shelly", f"pro3em_phase_{ph}_power", f"{mac}-em:0-{ph}_act_power", device_class="power", unit="W", device=f"shelly_{ph}", entry="shelly")
        r.add("shelly", f"pro3em_phase_{ph}_voltage", f"{mac}-em:0-{ph}_voltage", device_class="voltage", unit="V", device=f"shelly_{ph}", entry="shelly", disabled=True)
        r.add("shelly", f"pro3em_phase_{ph}_apparent", f"{mac}-em:0-{ph}_aprt_power", device_class="apparent_power", unit="VA", device=f"shelly_{ph}", entry="shelly")
        r.add("shelly", f"pro3em_phase_{ph}_pf", f"{mac}-em:0-{ph}_pf", device_class="power_factor", device=f"shelly_{ph}", entry="shelly")
    r.add("shelly", "pro3em_total_power", f"{mac}-em:0-total_act_power", device_class="power", unit="W", device="shelly_a", entry="shelly")
    return r


def shelly_gen1_3em() -> Registry:
    r = Registry()
    for index in range(3):
        r.add("shelly", f"shem3_ch{index}_current", f"aabbcc-emeter_{index}-current", device_class="current", unit="A", entry="shelly")
        r.add("shelly", f"shem3_ch{index}_power", f"aabbcc-emeter_{index}-power", device_class="power", unit="W", entry="shelly")
        r.add("shelly", f"shem3_ch{index}_voltage", f"aabbcc-emeter_{index}-voltage", device_class="voltage", unit="V", entry="shelly")
    return r


def shelly_em1_channels() -> Registry:
    r = Registry()
    for index in range(3):
        r.add("shelly", f"em_ch{index}_current", f"aabbcc-em1:{index}-current_em1", device_class="current", unit="A", entry="shelly")
        r.add("shelly", f"em_ch{index}_power", f"aabbcc-em1:{index}-power_em1", device_class="power", unit="W", entry="shelly")
        r.add("shelly", f"em_ch{index}_voltage", f"aabbcc-em1:{index}-voltage_em1", device_class="voltage", unit="V", entry="shelly")
    return r


def homewizard_p1() -> Registry:
    r = Registry()
    r.device("hw_p1", model="HWE-P1", manufacturer="HomeWizard", entry="hw")
    for n in "123":
        r.add("homewizard", f"p1_current_l{n}", f"5c2faf_active_current_l{n}_a", device_class="current", unit="A", device="hw_p1", entry="hw", disabled=True)
        r.add("homewizard", f"p1_power_l{n}", f"5c2faf_active_power_l{n}_w", device_class="power", unit="W", device="hw_p1", entry="hw")
        r.add("homewizard", f"p1_voltage_l{n}", f"5c2faf_active_voltage_l{n}_v", device_class="voltage", unit="V", device="hw_p1", entry="hw", disabled=True)
    r.add("homewizard", "p1_power", "5c2faf_active_power_w", device_class="power", unit="W", device="hw_p1", entry="hw")
    r.device("hw_bat", model="HWE-BAT", manufacturer="HomeWizard", entry="hwbat")
    r.add("homewizard", "battery_power", "aabb11_active_power_w", device_class="power", unit="W", device="hw_bat", entry="hwbat")
    return r


def tibber_pulse() -> Registry:
    r = Registry()
    for n in "123":
        r.add("tibber", f"tibber_current_l{n}", f"home1_rt_currentL{n}", device_class="current", unit="A", entry="tibber", translation_key=f"current_l{n}")
        r.add("tibber", f"tibber_voltage_l{n}", f"home1_rt_voltagePhase{n}", device_class="voltage", unit="V", entry="tibber")
    r.add("tibber", "tibber_power", "home1_rt_power", device_class="power", unit="W", entry="tibber")
    return r


def tibber_pulse_with_production() -> Registry:
    """A Pulse in a home with solar: core `tibber` creates `<home>_rt_power` (key `power`) and
    `<home>_rt_powerProduction` (key `powerProduction`) next to the per-phase current and voltage."""
    r = tibber_pulse()
    r.add("tibber", "tibber_power_production", "home1_rt_powerProduction", device_class="power", unit="W", entry="tibber", translation_key="power_production")
    return r


def dsmr_p1_with_totals() -> Registry:
    """Core `dsmr`: unique ids are `<serial>_<key>`; the totals are `current_electricity_usage` and
    `current_electricity_delivery`, in kW, next to the (disabled) per-phase entities."""
    r = dsmr_meter()
    r.add("dsmr", "dsmr_power_consumption", "SN1_current_electricity_usage", device_class="power", unit="kW", entry="dsmr", translation_key="current_electricity_usage")
    r.add("dsmr", "dsmr_power_production", "SN1_current_electricity_delivery", device_class="power", unit="kW", entry="dsmr", translation_key="current_electricity_delivery")
    return r


def dsmr_reader() -> Registry:
    """Core `dsmr_reader` (MQTT): unique ids are `<entry>-dsmr_reading_<topic>`, in kW; the per-phase
    entities ship disabled."""
    r = Registry()
    for n in "123":
        r.add("dsmr_reader", f"dsmr_reading_phase_currently_delivered_l{n}", f"dsrentry-dsmr_reading_phase_currently_delivered_l{n}", device_class="power", unit="kW", entry="dsr", disabled=True)
        r.add("dsmr_reader", f"dsmr_reading_phase_currently_returned_l{n}", f"dsrentry-dsmr_reading_phase_currently_returned_l{n}", device_class="power", unit="kW", entry="dsr", disabled=True)
        r.add("dsmr_reader", f"dsmr_reading_phase_voltage_l{n}", f"dsrentry-dsmr_reading_phase_voltage_l{n}", device_class="voltage", unit="V", entry="dsr", disabled=True)
        r.add("dsmr_reader", f"dsmr_reading_phase_power_current_l{n}", f"dsrentry-dsmr_reading_phase_power_current_l{n}", device_class="current", unit="A", entry="dsr", disabled=True)
    r.add("dsmr_reader", "dsmr_reading_electricity_currently_delivered", "dsrentry-dsmr_reading_electricity_currently_delivered", device_class="power", unit="kW", entry="dsr", translation_key="current_power_usage")
    r.add("dsmr_reader", "dsmr_reading_electricity_currently_returned", "dsrentry-dsmr_reading_electricity_currently_returned", device_class="power", unit="kW", entry="dsr", translation_key="current_power_return")
    return r


def p1_monitor() -> Registry:
    """Core `p1_monitor`: `<entry>_phases_<key>` for the per-phase values and `<entry>_smartmeter_<key>`
    for the total consumption and production, in W, on separate service devices of one entry."""
    r = Registry()
    for n in "123":
        r.add("p1_monitor", f"p1_current_phase_l{n}", f"p1e_phases_current_phase_l{n}", device_class="current", unit="A", entry="p1e", translation_key=f"current_phase_l{n}")
        r.add("p1_monitor", f"p1_voltage_phase_l{n}", f"p1e_phases_voltage_phase_l{n}", device_class="voltage", unit="V", entry="p1e", translation_key=f"voltage_phase_l{n}")
        r.add("p1_monitor", f"p1_power_consumed_phase_l{n}", f"p1e_phases_power_consumed_phase_l{n}", device_class="power", unit="W", entry="p1e", translation_key=f"power_consumed_phase_l{n}")
        r.add("p1_monitor", f"p1_power_produced_phase_l{n}", f"p1e_phases_power_produced_phase_l{n}", device_class="power", unit="W", entry="p1e", translation_key=f"power_produced_phase_l{n}")
    r.add("p1_monitor", "p1_power_consumption", "p1e_smartmeter_power_consumption", device_class="power", unit="W", entry="p1e", translation_key="power_consumption")
    r.add("p1_monitor", "p1_power_production", "p1e_smartmeter_power_production", device_class="power", unit="W", entry="p1e", translation_key="power_production")
    return r


def dsmr_meter() -> Registry:
    r = Registry()
    for n in "123":
        r.add("dsmr", f"dsmr_current_l{n}", f"SN1_instantaneous_current_l{n}", device_class="current", unit="A", entry="dsmr", disabled=True)
        r.add("dsmr", f"dsmr_import_l{n}", f"SN1_instantaneous_active_power_l{n}_positive", device_class="power", unit="kW", entry="dsmr", disabled=True)
        r.add("dsmr", f"dsmr_export_l{n}", f"SN1_instantaneous_active_power_l{n}_negative", device_class="power", unit="kW", entry="dsmr", disabled=True)
        r.add("dsmr", f"dsmr_voltage_l{n}", f"SN1_instantaneous_voltage_l{n}", device_class="voltage", unit="V", entry="dsmr", disabled=True)
    return r


def easee_equalizer() -> Registry:
    r = Registry()
    r.device("eq", model="Equalizer", manufacturer="Easee", name="Easee Equalizer", entry="easee")
    r.add("easee", "easee_equalizer_current", "QP123456_current", device_class="current", unit="A", device="eq", entry="easee", disabled=True)
    r.add("easee", "easee_equalizer_import_power", "QP123456_import_power", device_class="power", unit="kW", device="eq", entry="easee")
    return r


def huawei_solar() -> Registry:
    r = Registry()
    r.device("hw_meter", model="DTSU666-H", name="Power meter", entry="huawei")
    for ph in "ABC":
        r.add("huawei_solar", f"meter_current_{ph.lower()}", f"HV1_active_grid_{ph}_current", device_class="current", unit="A", device="hw_meter", entry="huawei", disabled=True)
        r.add("huawei_solar", f"meter_power_{ph.lower()}", f"HV1_active_grid_{ph}_power", device_class="power", unit="W", device="hw_meter", entry="huawei")
        r.add("huawei_solar", f"meter_voltage_{ph.lower()}", f"HV1_grid_{ph}_voltage", device_class="voltage", unit="V", device="hw_meter", entry="huawei", disabled=True)
    r.add("huawei_solar", "inverter_active_power", "HV1_active_power", device_class="power", unit="W", device="hw_meter", entry="huawei")
    r.add("huawei_solar", "battery_charge_discharge", "HV1_storage_charge_discharge_power", device_class="power", unit="W", device="hw_meter", entry="huawei")
    return r


def solarman_deye() -> Registry:
    r = Registry()
    for n in "123":
        r.add("solarman", f"deye_ct{n}_current", f"solarman_2312_external_ct{n}_current", device_class="current", unit="A", entry="solarman")
        r.add("solarman", f"deye_grid_l{n}_power", f"solarman_2312_grid_l{n}_power", device_class="power", unit="W", entry="solarman")
        r.add("solarman", f"deye_grid_l{n}_voltage", f"solarman_2312_grid_l{n}_voltage", device_class="voltage", unit="V", entry="solarman")
    r.add("solarman", "deye_battery_power", "solarman_2312_battery_power", device_class="power", unit="W", entry="solarman")
    return r


def solarman_core_p1() -> Registry:
    """Core `solarman` (a Solarman P1 meter): a different shape under the same domain."""
    r = Registry()
    for ph in "abc":
        r.add("solarman", f"p1_{ph}_power", f"2312-{ph}_act_power", device_class="power", unit="kW", entry="solarman_core")
    return r


def fronius_with_meter() -> Registry:
    r = Registry()
    r.device("fr_meter", model="Smart Meter TS 65A-3", manufacturer="Fronius", entry="fronius")
    r.device("fr_inv", model="Symo 10.0-3-M", manufacturer="Fronius", entry="fronius")
    for n in "123":
        r.add("fronius", f"meter_current_{n}", f"123-current_ac_phase_{n}", device_class="current", unit="A", device="fr_meter", entry="fronius", disabled=True)
        r.add("fronius", f"meter_power_{n}", f"123-power_real_phase_{n}", device_class="power", unit="W", device="fr_meter", entry="fronius", disabled=True)
        r.add("fronius", f"meter_reactive_{n}", f"123-power_reactive_phase_{n}", device_class="reactive_power", unit="var", device="fr_meter", entry="fronius", disabled=True)
        r.add("fronius", f"meter_voltage_{n}", f"123-voltage_ac_phase_{n}", device_class="voltage", unit="V", device="fr_meter", entry="fronius", disabled=True)
    r.add("fronius", "inverter_power_battery", "456-power_battery", device_class="power", unit="W", device="fr_inv", entry="fronius")
    return r


def enphase_envoy(*, installer_override: bool = False) -> Registry:
    r = Registry()
    for n in "123":
        r.add("enphase_envoy", f"envoy_net_consumption_l{n}", f"ENV1_net_consumption_l{n}", device_class="power", unit="W", entry="envoy")
        r.add("enphase_envoy", f"envoy_net_current_l{n}", f"ENV1_net_ct_current_l{n}", device_class="current", unit="A", entry="envoy", disabled=True)
        r.add("enphase_envoy", f"envoy_voltage_l{n}", f"ENV1_voltage_l{n}", device_class="voltage", unit="V", entry="envoy", disabled=True)
    if installer_override:
        for n in "123":
            r.add("enphase_envoy", f"envoy_production_current_l{n}", f"ENV1_production_ct_current_l{n}", device_class="current", unit="A", entry="envoy")
            r.add("enphase_envoy", f"envoy_production_voltage_l{n}", f"ENV1_production_ct_voltage_l{n}", device_class="voltage", unit="V", entry="envoy")
            r.add("enphase_envoy", f"envoy_production_power_l{n}", f"ENV1_production_ct_power_l{n}", device_class="power", unit="W", entry="envoy")
    r.add("enphase_envoy", "envoy_battery_discharge", "ENV1_battery_discharge", device_class="power", unit="W", entry="envoy")
    return r


def sma_with_inverter() -> Registry:
    r = Registry()
    for n in "123":
        r.add("sma", f"sma_em_current_l{n}", f"sma_1_metering_current_l{n}", device_class="current", unit="A", entry="sma")
        r.add("sma", f"sma_em_draw_l{n}", f"sma_1_metering_active_power_draw_l{n}", device_class="power", unit="W", entry="sma")
        r.add("sma", f"sma_em_feed_l{n}", f"sma_1_metering_active_power_feed_l{n}", device_class="power", unit="W", entry="sma")
        r.add("sma", f"sma_em_voltage_l{n}", f"sma_1_metering_voltage_l{n}", device_class="voltage", unit="V", entry="sma")
        # The inverter's own output: per-phase reactive power is not the grid's.
        r.add("sma", f"sma_inv_grid_reactive_power_l{n}", f"sma_1_grid_reactive_power_l{n}", device_class="reactive_power", unit="var", entry="sma")
    r.add("sma", "sma_inv_grid_power", "sma_1_grid_power", device_class="power", unit="W", entry="sma")
    r.add("sma", "sma_battery_charge", "sma_1_battery_power_charge", device_class="power", unit="W", entry="sma")
    r.add("sma", "sma_battery_discharge", "sma_1_battery_power_discharge", device_class="power", unit="W", entry="sma")
    return r


def solaredge_modbus_multi(meters=("m1",)) -> Registry:
    r = Registry()
    for meter in meters:
        for ph in "abc":
            r.add("solaredge_modbus_multi", f"se_{meter}_current_{ph}", f"se_7e1_{meter}_ac_current_{ph}", device_class="current", unit="A", entry="se")
            r.add("solaredge_modbus_multi", f"se_{meter}_power_{ph}", f"se_7e1_{meter}_ac_power_{ph}", device_class="power", unit="W", entry="se")
            r.add("solaredge_modbus_multi", f"se_{meter}_voltage_{ph}", f"se_7e1_{meter}_ac_voltage_{ph}n", device_class="voltage", unit="V", entry="se")
            r.add("solaredge_modbus_multi", f"se_{meter}_var_{ph}", f"se_7e1_{meter}_ac_var_{ph}", device_class="reactive_power", unit="var", entry="se", disabled=True)
    # The inverter's own AC output shares the suffixes but is not a meter.
    for ph in "abc":
        r.add("solaredge_modbus_multi", f"se_i1_current_{ph}", f"se_7e1_i1_ac_current_{ph}", device_class="current", unit="A", entry="se")
    r.add("solaredge_modbus_multi", "se_b1_dc_power", "se_7e1_b1_dc_power", device_class="power", unit="W", entry="se")
    return r


def victron_gx() -> Registry:
    r = Registry()
    for n in "123":
        r.add("victron_gx", f"gx_grid_current_l{n}", f"gx1_grid_current_L{n}", device_class="current", unit="A", entry="gx")
        r.add("victron_gx", f"gx_grid_power_l{n}", f"gx1_grid_power_L{n}", device_class="power", unit="W", entry="gx")
        r.add("victron_gx", f"gx_grid_voltage_l{n}", f"gx1_grid_voltage_L{n}", device_class="voltage", unit="V", entry="gx")
    r.add("victron_gx", "gx_battery_power", "gx1_system_dc_battery_power", device_class="power", unit="W", entry="gx")
    return r


def goodwe(*, large_et: bool) -> Registry:
    r = Registry()
    for n in "123":
        r.add("goodwe", f"gw_meter_power_{n}", f"SN9-meter_active_power{n}", device_class="power", unit="W", entry="gw")
        r.add("goodwe", f"gw_meter_reactive_{n}", f"SN9-meter_reactive_power{n}", device_class="reactive_power", unit="var", entry="gw", disabled=True)
        if large_et:
            r.add("goodwe", f"gw_meter_current_{n}", f"SN9-meter_current{n}", device_class="current", unit="A", entry="gw")
            r.add("goodwe", f"gw_meter_voltage_{n}", f"SN9-meter_voltage{n}", device_class="voltage", unit="V", entry="gw")
    r.add("goodwe", "gw_battery_power", "SN9-pbattery1", device_class="power", unit="W", entry="gw")
    return r


def sigen() -> Registry:
    r = Registry()
    r.device("sg_plant", model="Plant", name="Sigen Plant", entry="sigen")
    r.device("sg_inv", model="Inverter", name="Sigen Inverter", entry="sigen")
    for ph in "abc":
        r.add("sigen", f"sigen_plant_grid_phase_{ph}_active", f"sigen_0_plant_grid_sensor_phase_{ph}_active_power", device_class="power", unit="kW", device="sg_plant", entry="sigen", disabled=True)
        r.add("sigen", f"sigen_plant_grid_phase_{ph}_reactive", f"sigen_0_plant_grid_sensor_phase_{ph}_reactive_power", device_class="reactive_power", unit="kvar", device="sg_plant", entry="sigen", disabled=True)
        r.add("sigen", f"sigen_inverter_phase_{ph}_voltage", f"sigen_1_inverter_phase_{ph}_voltage", device_class="voltage", unit="V", device="sg_inv", entry="sigen")
    r.add("sigen", "sigen_plant_ess_power", "sigen_0_plant_ess_power", device_class="power", unit="kW", device="sg_plant", entry="sigen")
    return r


def ams_reader(*, list4: bool) -> Registry:
    r = Registry()
    r.device("ams", model="Pow-K", manufacturer="amsleser.no", name="AMS reader", entry="mqtt")
    for n in "123":
        r.add("mqtt", f"ams_i{n}", f"amsreader_ab12_I{n}", device_class="current", unit="A", device="ams", entry="mqtt")
        r.add("mqtt", f"ams_u{n}", f"amsreader_ab12_U{n}", device_class="voltage", unit="V", device="ams", entry="mqtt")
        if list4:
            r.add("mqtt", f"ams_p{n}", f"amsreader_ab12_P{n}", device_class="power", unit="W", device="ams", entry="mqtt")
            r.add("mqtt", f"ams_po{n}", f"amsreader_ab12_PO{n}", device_class="power", unit="W", device="ams", entry="mqtt")
    return r


def slimmelezer() -> Registry:
    r = Registry()
    r.device("sl", model="slimmelezer", manufacturer="zuidwijk", name="SlimmeLezer", entry="esphome")
    for n in "123":
        r.add("esphome", f"slimmelezer_current_phase_{n}", f"a1b2c3-sensor-current_l{n}", device_class="current", unit="A", device="sl", entry="esphome", original_name=f"Current Phase {n}")
        r.add("esphome", f"slimmelezer_power_delivered_phase_{n}", f"a1b2c3-sensor-power_delivered_l{n}", device_class="power", unit="kW", device="sl", entry="esphome", original_name=f"Power Delivered Phase {n}")
        r.add("esphome", f"slimmelezer_power_returned_phase_{n}", f"a1b2c3-sensor-power_returned_l{n}", device_class="power", unit="kW", device="sl", entry="esphome", original_name=f"Power Returned Phase {n}")
        r.add("esphome", f"slimmelezer_voltage_phase_{n}", f"a1b2c3-sensor-voltage_l{n}", device_class="voltage", unit="V", device="sl", entry="esphome", original_name=f"Voltage Phase {n}")
        r.add("esphome", f"slimmelezer_reactive_delivered_phase_{n}", f"a1b2c3-sensor-reactive_power_delivered_l{n}", device_class="reactive_power", unit="var", device="sl", entry="esphome")
    return r


# ---- the table ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Expect:
    mode: str
    signed_current: bool = False
    power_inverted: bool = False
    estimated: bool = False
    roles: frozenset[str] = frozenset()
    disabled: int = 0
    integration: str = ""


CASES = {
    "shelly_pro_3em": (
        shelly_pro_3em,
        Expect(MEASUREMENT_MODE_DERIVED, roles=frozenset({"power", "voltage", "current", "apparent_power", "grid_power"}), disabled=6, integration="shelly"),
    ),
    "shelly_gen1_3em": (
        shelly_gen1_3em,
        Expect(MEASUREMENT_MODE_DERIVED, roles=frozenset({"power", "voltage", "current"}), integration="shelly"),
    ),
    "shelly_em1_channels": (
        shelly_em1_channels,
        Expect(MEASUREMENT_MODE_DERIVED, roles=frozenset({"power", "voltage", "current"}), integration="shelly"),
    ),
    "homewizard_p1": (
        homewizard_p1,
        Expect(MEASUREMENT_MODE_DERIVED, signed_current=True, roles=frozenset({"power", "voltage", "current", "grid_power"}), disabled=6, integration="homewizard"),
    ),
    "tibber": (
        tibber_pulse,
        Expect(MEASUREMENT_MODE_DIRECT, roles=frozenset({"current"}), integration="tibber"),
    ),
    "tibber_with_production": (
        tibber_pulse_with_production,
        Expect(MEASUREMENT_MODE_DIRECT, roles=frozenset({"current", "grid_power", "grid_power_export"}), integration="tibber"),
    ),
    "dsmr_totals": (
        dsmr_p1_with_totals,
        Expect(MEASUREMENT_MODE_DERIVED, roles=frozenset({"power", "power_export", "voltage", "current", "grid_power", "grid_power_export"}), disabled=12, integration="dsmr"),
    ),
    "dsmr_reader": (
        dsmr_reader,
        Expect(MEASUREMENT_MODE_DERIVED, roles=frozenset({"power", "power_export", "voltage", "current", "grid_power", "grid_power_export"}), disabled=12, integration="dsmr_reader"),
    ),
    "p1_monitor": (
        p1_monitor,
        Expect(MEASUREMENT_MODE_DERIVED, roles=frozenset({"power", "power_export", "voltage", "current", "grid_power", "grid_power_export"}), integration="p1_monitor"),
    ),
    "dsmr": (
        dsmr_meter,
        Expect(MEASUREMENT_MODE_DERIVED, roles=frozenset({"power", "power_export", "voltage", "current"}), disabled=12, integration="dsmr"),
    ),
    "huawei_solar": (
        huawei_solar,
        Expect(MEASUREMENT_MODE_DERIVED, signed_current=True, power_inverted=True, roles=frozenset({"power", "voltage", "current"}), disabled=6, integration="huawei_solar"),
    ),
    "solarman_deye": (
        solarman_deye,
        Expect(MEASUREMENT_MODE_DERIVED, signed_current=True, roles=frozenset({"power", "voltage", "current"}), integration="solarman"),
    ),
    "fronius": (
        fronius_with_meter,
        Expect(MEASUREMENT_MODE_DERIVED, signed_current=True, roles=frozenset({"power", "voltage", "current", "reactive_power"}), disabled=12, integration="fronius"),
    ),
    "enphase_envoy": (
        enphase_envoy,
        Expect(MEASUREMENT_MODE_DERIVED, roles=frozenset({"power", "voltage", "current"}), disabled=6, integration="enphase_envoy"),
    ),
    "enphase_installer_override_production_ct_ignored": (
        lambda: enphase_envoy(installer_override=True),
        Expect(MEASUREMENT_MODE_DERIVED, roles=frozenset({"power", "voltage", "current"}), disabled=6, integration="enphase_envoy"),
    ),
    "sma": (
        sma_with_inverter,
        Expect(MEASUREMENT_MODE_DERIVED, roles=frozenset({"power", "power_export", "voltage", "current"}), integration="sma"),
    ),
    "solaredge_modbus_multi": (
        solaredge_modbus_multi,
        Expect(MEASUREMENT_MODE_DERIVED, signed_current=True, power_inverted=True, roles=frozenset({"power", "voltage", "current", "reactive_power"}), disabled=3, integration="solaredge_modbus_multi"),
    ),
    "victron_gx": (
        victron_gx,
        Expect(MEASUREMENT_MODE_DERIVED, signed_current=True, roles=frozenset({"power", "voltage", "current"}), integration="victron_gx"),
    ),
    "goodwe_large_et": (
        lambda: goodwe(large_et=True),
        Expect(MEASUREMENT_MODE_DERIVED, power_inverted=True, roles=frozenset({"power", "voltage", "current", "reactive_power"}), disabled=3, integration="goodwe"),
    ),
    "sigen": (
        sigen,
        Expect(MEASUREMENT_MODE_DERIVED, roles=frozenset({"power", "voltage", "reactive_power"}), disabled=6, integration="sigen"),
    ),
    "ams_reader_list4": (
        lambda: ams_reader(list4=True),
        Expect(MEASUREMENT_MODE_DERIVED, roles=frozenset({"power", "power_export", "voltage", "current"}), integration="mqtt"),
    ),
    "ams_reader_list1": (
        lambda: ams_reader(list4=False),
        Expect(MEASUREMENT_MODE_DIRECT, roles=frozenset({"current"}), integration="mqtt"),
    ),
    "slimmelezer": (
        slimmelezer,
        Expect(MEASUREMENT_MODE_DERIVED, roles=frozenset({"power", "power_export", "voltage", "current"}), integration="esphome"),
    ),
    "easee_equalizer": (
        easee_equalizer,
        Expect(MEASUREMENT_MODE_DIRECT, roles=frozenset({"current"}), disabled=1, integration="easee"),
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_each_integration_is_detected_with_its_mode_entities_and_sign_fixes(name: str) -> None:
    build, expect = CASES[name]

    candidate = one_meter(build())

    assert candidate.integration == expect.integration
    assert candidate.mode == expect.mode
    assert candidate.signed_current is expect.signed_current
    assert candidate.power_inverted is expect.power_inverted
    assert candidate.confidence == "high"
    assert set(roles(candidate)) == set(expect.roles)
    assert len(candidate.disabled_entity_ids) == expect.disabled
    assert candidate.estimated is expect.estimated


def test_shelly_pro_3em_uses_apparent_power_so_it_is_not_estimated_and_lists_what_to_enable() -> None:
    candidate = one_meter(shelly_pro_3em())

    assert candidate.derived_entities["L2"] == {
        "power": "sensor.pro3em_phase_b_power",
        "voltage": "sensor.pro3em_phase_b_voltage",
        "current": "sensor.pro3em_phase_b_current",
        "apparent_power": "sensor.pro3em_phase_b_apparent",
    }
    assert not candidate.estimated
    assert set(candidate.disabled_entity_ids) == {
        f"sensor.pro3em_phase_{ph}_{kind}" for ph in "abc" for kind in ("current", "voltage")
    }
    assert WARNING_MAY_MEASURE_SUBCIRCUIT in candidate.warnings


def test_the_shelly_total_power_and_power_factor_entities_are_not_picked_as_phases() -> None:
    candidate = one_meter(shelly_pro_3em())

    assert "sensor.pro3em_total_power" not in {
        item.entity_id for item in candidate.entities if item.phase is not None
    }
    assert not any(item.entity_id.endswith("_pf") for item in candidate.entities)


# ---- the meter's total grid power ---------------------------------------------------------------


def test_the_shelly_gen2_total_active_power_is_the_one_signed_total() -> None:
    candidate = one_meter(shelly_pro_3em())

    assert candidate.grid_power == GridPowerSource(power="sensor.pro3em_total_power")
    assert [(item.role, item.phase) for item in candidate.entities if item.role == "grid_power"] == [
        ("grid_power", None)
    ]


def test_the_shelly_gen1_and_em1_shapes_have_no_total_to_detect() -> None:
    assert one_meter(shelly_gen1_3em()).grid_power is None
    assert one_meter(shelly_em1_channels()).grid_power is None


def test_the_homewizard_total_is_the_meter_and_never_the_batterys_active_power() -> None:
    detection = homewizard_p1().detect()

    assert detection.meters[0].grid_power == GridPowerSource(power="sensor.p1_power")
    assert "sensor.battery_power" not in {item.entity_id for item in detection.meters[0].entities}
    # The same unique-id suffix on the battery device is its own power, a battery candidate only.
    assert [b.entity_id for b in detection.batteries] == ["sensor.battery_power"]


def test_a_homewizard_battery_alone_is_not_a_meter_total() -> None:
    r = Registry()
    r.device("hw_bat", model="Plug-In Battery", manufacturer="HomeWizard", entry="hw")
    for n in "123":
        r.add("homewizard", f"b_current_l{n}", f"aabb_active_current_l{n}_a", device_class="current", unit="A", device="hw_bat", entry="hw")
    r.add("homewizard", "b_power", "aabb_active_power_w", device_class="power", unit="W", device="hw_bat", entry="hw")

    candidates = [c for c in r.detect().meters if c.integration == "homewizard"]

    assert all(candidate.grid_power is None for candidate in candidates)


def test_tibber_pairs_consumption_and_production_as_import_and_export() -> None:
    candidate = one_meter(tibber_pulse_with_production())

    assert candidate.mode == MEASUREMENT_MODE_DIRECT
    assert candidate.grid_power == GridPowerSource(
        power="sensor.tibber_power", power_export="sensor.tibber_power_production"
    )
    assert candidate.power_inverted is False


def test_tibber_without_a_production_sensor_has_no_total_because_import_alone_never_exports() -> None:
    candidate = one_meter(tibber_pulse())

    assert candidate.grid_power is None
    assert "grid_power" not in roles(candidate)


def test_the_tibber_averages_and_extremes_are_not_the_total() -> None:
    r = tibber_pulse_with_production()
    for key in ("averagePower", "minPower", "maxPower", "powerFactor", "accumulatedProduction"):
        r.add("tibber", f"tibber_{key.lower()}", f"home1_rt_{key}", device_class="power", unit="W", entry="tibber")

    candidate = one_meter(r)

    assert candidate.grid_power == GridPowerSource(
        power="sensor.tibber_power", power_export="sensor.tibber_power_production"
    )


def test_a_production_sensor_without_its_consumption_half_makes_no_total() -> None:
    r = tibber_pulse()
    r.entities = [e for e in r.entities if not e.unique_id.endswith("_rt_power")]
    r.add("tibber", "tibber_power_production", "home1_rt_powerProduction", device_class="power", unit="W", entry="tibber")

    assert one_meter(r).grid_power is None


def test_dsmr_totals_are_usage_and_delivery_as_a_pair() -> None:
    candidate = one_meter(dsmr_p1_with_totals())

    assert candidate.grid_power == GridPowerSource(
        power="sensor.dsmr_power_consumption", power_export="sensor.dsmr_power_production"
    )
    assert candidate.mode == MEASUREMENT_MODE_DERIVED


def test_dsmr_reader_totals_are_currently_delivered_and_returned_not_the_phases() -> None:
    candidate = one_meter(dsmr_reader())

    assert candidate.grid_power == GridPowerSource(
        power="sensor.dsmr_reading_electricity_currently_delivered",
        power_export="sensor.dsmr_reading_electricity_currently_returned",
    )
    assert candidate.derived_entities["L1"]["power"] == "sensor.dsmr_reading_phase_currently_delivered_l1"


def test_p1_monitor_totals_come_from_the_smart_meter_service_of_the_same_entry() -> None:
    candidate = one_meter(p1_monitor())

    assert candidate.grid_power == GridPowerSource(
        power="sensor.p1_power_consumption", power_export="sensor.p1_power_production"
    )
    assert candidate.derived_entities["L2"]["power"] == "sensor.p1_power_consumed_phase_l2"


def test_totals_never_make_a_meter_of_their_own() -> None:
    r = Registry()
    r.add("tibber", "tibber_power", "home1_rt_power", device_class="power", unit="W", entry="tibber")
    r.add("tibber", "tibber_power_production", "home1_rt_powerProduction", device_class="power", unit="W", entry="tibber")
    r.add("dsmr", "dsmr_usage", "SN1_current_electricity_usage", device_class="power", unit="kW", entry="dsmr")
    r.add("dsmr", "dsmr_delivery", "SN1_current_electricity_delivery", device_class="power", unit="kW", entry="dsmr")

    assert r.detect().meters == ()


def test_totals_of_two_entries_stay_with_their_own_meter() -> None:
    r = tibber_pulse_with_production()
    other = dsmr_p1_with_totals()
    r.entities += other.entities

    detection = r.detect()

    by_integration = {candidate.integration: candidate for candidate in detection.meters}
    assert by_integration["tibber"].grid_power.power == "sensor.tibber_power"
    assert by_integration["dsmr"].grid_power.power == "sensor.dsmr_power_consumption"


def test_the_total_of_a_meter_with_inversion_is_not_claimed_by_a_catalogue_row_without_one() -> None:
    # Huawei, SolarEdge, GoodWe and SolaX have no total row: SolaX's `measured_power` is signed
    # inconsistently in its own source, so it is left out rather than guessed.
    r = Registry()
    r.device("sx", model="X3", manufacturer="Solax", entry="solax")
    for n in "123":
        r.add("solax_modbus", f"sx_measured_power_l{n}", f"SolaX_measured_power_l{n}", device_class="power", unit="W", device="sx", entry="solax")
        r.add("solax_modbus", f"sx_grid_voltage_l{n}", f"SolaX_grid_voltage_l{n}", device_class="voltage", unit="V", device="sx", entry="solax")
    r.add("solax_modbus", "sx_measured_power", "SolaX_measured_power", device_class="power", unit="W", device="sx", entry="solax")

    candidate = one_meter(r)

    assert candidate.power_inverted is True
    assert candidate.grid_power is None


def test_homewizard_p1_reads_its_signed_current_as_a_magnitude_and_ignores_the_battery_device() -> None:
    registry = homewizard_p1()

    detection = registry.detect()

    assert len(detection.meters) == 1
    assert detection.meters[0].signed_current is True
    assert [b.entity_id for b in detection.batteries] == ["sensor.battery_power"]
    assert detection.batteries[0].inverted is False


def test_dsmr_split_power_becomes_an_import_export_pair() -> None:
    candidate = one_meter(dsmr_meter())

    assert candidate.derived_entities["L1"] == {
        "power": "sensor.dsmr_import_l1",
        "power_export": "sensor.dsmr_export_l1",
        "voltage": "sensor.dsmr_voltage_l1",
        "current": "sensor.dsmr_current_l1",
    }


def test_tibber_has_no_per_phase_power_so_it_is_a_direct_current_source() -> None:
    candidate = one_meter(tibber_pulse())

    assert candidate.direct_entities == {ph: f"sensor.tibber_current_l{ph[1]}" for ph in PHASES}
    assert candidate.derived_entities is None


def test_easee_equalizer_is_an_attributes_source_and_warns_it_balances_by_itself() -> None:
    candidate = one_meter(easee_equalizer())

    source = candidate.current_source
    assert source is not None and source.kind == "attributes"
    assert source.entity_id == "sensor.easee_equalizer_current"
    assert source.attributes == {"L1": "state_currentL1", "L2": "state_currentL2", "L3": "state_currentL3"}
    assert source.attribute_unit_override == "A"
    assert WARNING_OWN_LOAD_BALANCING in candidate.warnings
    assert candidate.disabled_entity_ids == ("sensor.easee_equalizer_current",)


def test_huawei_grid_power_is_inverted_and_its_inverter_output_is_not_a_phase() -> None:
    candidate = one_meter(huawei_solar())

    assert candidate.power_inverted is True
    assert "sensor.inverter_active_power" not in {item.entity_id for item in candidate.entities}


def test_fronius_needs_the_meter_device_and_ignores_the_inverter() -> None:
    registry = fronius_with_meter()
    # The same per-phase entities on the inverter device are not a grid meter.
    for entity in list(registry.entities):
        if entity.device_id == "fr_meter":
            registry.entities.append(
                RegistryEntity(
                    entity_id=entity.entity_id.replace("meter_", "inv_"),
                    platform=entity.platform,
                    unique_id=entity.unique_id.replace("123-", "456-"),
                    device_class=entity.device_class,
                    unit=entity.unit,
                    device_id="fr_inv",
                    config_entry_id="fronius_inv",
                    disabled_by=entity.disabled_by,
                )
            )

    detection = registry.detect()

    assert [c.integration for c in detection.meters] == ["fronius"]
    assert all("inv_" not in item.entity_id for item in detection.meters[0].entities)


def test_the_enphase_production_ct_is_never_used_as_the_grid() -> None:
    candidate = one_meter(enphase_envoy(installer_override=True))

    assert not any("production" in item.entity_id for item in candidate.entities)


def test_sma_takes_the_meter_and_not_the_inverters_grid_power() -> None:
    candidate = one_meter(sma_with_inverter())

    used = {item.entity_id for item in candidate.entities}
    assert "sensor.sma_inv_grid_power" not in used
    assert not any("reactive" in entity_id for entity_id in used)


def test_solaredge_modbus_multi_picks_the_meter_not_the_inverter_and_one_candidate_per_meter() -> None:
    two = solaredge_modbus_multi(("m1", "m2"))

    detection = two.detect()

    assert len(detection.meters) == 2
    for candidate in detection.meters:
        assert not any("_i1_" in item.entity_id for item in candidate.entities)
    assert {c.candidate_id.rsplit(":", 1)[-1] for c in detection.meters} == {"m1", "m2"}


def test_goodwe_without_voltage_is_not_guessed_but_a_large_et_is_detected() -> None:
    assert goodwe(large_et=False).detect().meters == ()
    candidate = one_meter(goodwe(large_et=True))
    assert candidate.power_inverted is True


def test_sigen_gets_its_voltage_from_the_inverter_and_says_so() -> None:
    candidate = one_meter(sigen())

    assert candidate.derived_entities["L1"] == {
        "power": "sensor.sigen_plant_grid_phase_a_active",
        "reactive_power": "sensor.sigen_plant_grid_phase_a_reactive",
        "voltage": "sensor.sigen_inverter_phase_a_voltage",
    }
    assert WARNING_VOLTAGE_OTHER_DEVICE in candidate.warnings
    assert not candidate.estimated


def test_the_sigen_candidate_writes_the_same_derived_shape_a_manual_site_stores() -> None:
    candidate = one_meter(sigen())

    data = apply_meter_candidate({"measurement_mode": "direct_phase_current"}, candidate)

    assert data["measurement_mode"] == MEASUREMENT_MODE_DERIVED
    assert data["derived_entities"]["L2"] == {
        "power": "sensor.sigen_plant_grid_phase_b_active",
        "reactive_power": "sensor.sigen_plant_grid_phase_b_reactive",
        "voltage": "sensor.sigen_inverter_phase_b_voltage",
    }
    assert data["grid_power_inverted"] is False and data["site_current_signed"] is False


def test_an_ams_reader_is_recognized_by_its_manufacturer_not_by_the_mqtt_platform() -> None:
    plain_mqtt = ams_reader(list4=True)
    plain_mqtt.devices[0] = RegistryDevice("ams", manufacturer="someone else", model="x", config_entry_ids=("mqtt",))

    assert plain_mqtt.detect().meters == ()


def test_a_p1_reader_is_detected_whatever_its_esphome_device_model_and_ignores_reactive_power() -> None:
    reader = slimmelezer()
    reader.devices[0] = RegistryDevice("sl", manufacturer="espressif", model="esp32dev", config_entry_ids=("esphome",))

    candidate = one_meter(reader)

    assert candidate.integration == "esphome"
    assert candidate.derived_entities["L3"]["power"] == "sensor.slimmelezer_power_delivered_phase_3"
    assert candidate.derived_entities["L3"]["power_export"] == "sensor.slimmelezer_power_returned_phase_3"
    assert "reactive_power" not in candidate.derived_entities["L3"]


def test_an_esphome_device_with_a_single_current_sensor_is_not_a_grid_meter() -> None:
    r = Registry()
    r.device("d", model="esp32dev", manufacturer="espressif", name="Garage", entry="esphome")
    r.add("esphome", "garage_current", "x-sensor-current", device_class="current", unit="A", device="d", entry="esphome")

    assert r.detect().meters == ()


def test_a_real_grid_meter_is_listed_before_the_easee_equalizer() -> None:
    both = slimmelezer()
    both.devices[0] = RegistryDevice("sl", manufacturer="espressif", model="esp32dev", config_entry_ids=("esphome",))
    eq = easee_equalizer()
    both.devices.extend(eq.devices)
    both.entities.extend(eq.entities)

    meters = both.detect().meters

    assert [m.integration for m in meters] == ["esphome", "easee"]


def test_the_core_solarman_p1_shape_is_not_taken_for_the_hacs_inverter_integration() -> None:
    assert solarman_core_p1().detect().meters == ()


# ---- derived shapes -----------------------------------------------------------------------------


def test_a_meter_with_only_power_and_voltage_is_estimated() -> None:
    r = Registry()
    for n in "123":
        r.add("victron_gx", f"p{n}", f"gx_grid_power_L{n}", device_class="power", unit="W", entry="gx")
        r.add("victron_gx", f"v{n}", f"gx_grid_voltage_L{n}", device_class="voltage", unit="V", entry="gx")

    candidate = one_meter(r)

    assert candidate.mode == MEASUREMENT_MODE_DERIVED
    assert candidate.estimated is True


def test_a_role_with_fewer_than_three_phases_is_left_out_not_half_used() -> None:
    r = victron_gx()
    r.entities = [e for e in r.entities if not e.entity_id.endswith("current_l3")]

    candidate = one_meter(r)

    assert "current" not in roles(candidate)
    assert candidate.estimated is True


# ---- registry scan: disabled entities -----------------------------------------------------------


def test_entities_the_integration_ships_disabled_are_offered_for_enabling() -> None:
    candidate = one_meter(homewizard_p1())

    disabled = {item.entity_id for item in candidate.entities if item.disabled}
    assert disabled == set(candidate.disabled_entity_ids)
    assert len(disabled) == 6


def test_an_entity_a_person_disabled_is_never_used_or_offered() -> None:
    r = homewizard_p1()
    r.entities = [
        replace(e, disabled_by="user") if e.entity_id == "sensor.p1_current_l2" else e
        for e in r.entities
    ]

    candidate = one_meter(r)

    assert "current" not in roles(candidate)  # incomplete without L2: not half used
    assert "sensor.p1_current_l2" not in candidate.disabled_entity_ids
    assert candidate.estimated is True


def test_a_charger_device_is_never_suggested_as_the_site_meter() -> None:
    r = huawei_solar()

    assert r.detect(excluded_device_ids={"hw_meter"}).meters == ()


# ---- generic fallback ---------------------------------------------------------------------------


def test_frient_emi_phase_a_without_a_suffix_is_found_by_its_ph_b_and_ph_c_siblings() -> None:
    r = Registry()
    r.device("frient", model="EMIZB-132", manufacturer="frient", name="frient EMI", entry="zha")
    r.add("zha", "frient_emi_rms_current", "00:0d:6f-2-2820", device_class="current", unit="A", device="frient", entry="zha")
    r.add("zha", "frient_emi_rms_current_ph_b", "00:0d:6f-2-2820-ph_b", device_class="current", unit="A", device="frient", entry="zha")
    r.add("zha", "frient_emi_rms_current_ph_c", "00:0d:6f-2-2820-ph_c", device_class="current", unit="A", device="frient", entry="zha")

    candidate = one_meter(r)

    assert candidate.mode == MEASUREMENT_MODE_DIRECT
    assert candidate.confidence in ("medium", "low")
    assert candidate.direct_entities == {
        "L1": "sensor.frient_emi_rms_current",
        "L2": "sensor.frient_emi_rms_current_ph_b",
        "L3": "sensor.frient_emi_rms_current_ph_c",
    }


def test_zigbee2mqtt_phase_b_and_c_names_are_recognised() -> None:
    r = Registry()
    r.device("z2m", model="EMIZB-132", name="Z2M EMI", entry="mqtt")
    for suffix, name in (("", "current"), ("_phase_b", "current_phase_b"), ("_phase_c", "current_phase_c")):
        r.add("mqtt", f"z2m_emi_{name}", f"0x00_{name}_zigbee2mqtt", device_class="current", unit="A", device="z2m", entry="mqtt")

    candidate = one_meter(r)

    assert candidate.direct_entities["L3"] == "sensor.z2m_emi_current_phase_c"
    assert candidate.direct_entities["L1"] == "sensor.z2m_emi_current"


def test_a_bare_phase_name_without_siblings_is_not_a_phase() -> None:
    r = Registry()
    r.device("one", entry="zha")
    r.add("zha", "meter_current", "x-1", device_class="current", unit="A", device="one", entry="zha")

    assert r.detect().meters == ()


def test_r_s_t_naming_on_a_grid_meter_is_a_complete_trio_only() -> None:
    r = Registry()
    r.device("foxmeter", name="Grid meter", entry="fox")
    for letter in "rst":
        r.add("foxess_unknown", f"grid_meter_current_{letter}", f"fx_meter_current_{letter}", device_class="current", unit="A", device="foxmeter", entry="fox")
    candidate = one_meter(r)
    assert candidate.direct_entities == {ph: f"sensor.grid_meter_current_{letter}" for ph, letter in zip(PHASES, "rst")}

    partial = Registry()
    partial.device("foxmeter", entry="fox")
    for letter in "rs":
        partial.add("foxess_unknown", f"grid_meter_current_{letter}", f"fx_{letter}", device_class="current", unit="A", device="foxmeter", entry="fox")
    assert partial.detect().meters == ()


def test_phase_a_b_c_and_l1_l2_l3_tokens_are_recognised() -> None:
    lettered = Registry()
    lettered.device("m", name="Grid meter", entry="x")
    for ph in "abc":
        lettered.add("somebrand", f"grid_meter_current_phase_{ph}", f"u_{ph}", device_class="current", unit="A", device="m", entry="x")
    assert one_meter(lettered).direct_entities["L2"] == "sensor.grid_meter_current_phase_b"

    numbered = Registry()
    numbered.device("m", name="Grid meter", entry="x")
    for n in "123":
        numbered.add("somebrand", f"grid_meter_current_l{n}", f"u_{n}", device_class="current", unit="A", device="m", entry="x")
    assert one_meter(numbered).direct_entities["L3"] == "sensor.grid_meter_current_l3"


@pytest.mark.parametrize(
    "template",
    [
        "inverter_phase_{n}_current",
        "solax_output_current_l{n}",
        "pv_inverter_current_l{n}",
        "inverter_ac_current_l{n}",
        "house_load_current_l{n}",
    ],
)
def test_inverter_output_per_phase_values_are_rejected(template: str) -> None:
    r = Registry()
    r.device("inv", entry="x")
    for n in "123":
        r.add("somebrand", template.format(n=n), f"u_{n}", device_class="current", unit="A", device="inv", entry="x")

    assert r.detect().meters == ()


def test_the_ac_current_r_s_t_inverter_naming_is_rejected() -> None:
    r = Registry()
    r.device("inv", entry="x")
    for letter in "rst":
        r.add("somebrand", f"inverter_ac_current_{letter}", f"u_{letter}", device_class="current", unit="A", device="inv", entry="x")
    r.add("somebrand", "ac_current_r", "x_r", device_class="current", unit="A", device="inv", entry="x")

    assert r.detect().meters == ()


def test_an_inverter_name_is_fine_when_the_entity_is_also_named_grid() -> None:
    r = Registry()
    r.device("inv", entry="x")
    for n in "123":
        r.add("somebrand", f"inverter_grid_current_l{n}", f"u_{n}", device_class="current", unit="A", device="inv", entry="x")

    assert one_meter(r).direct_entities["L1"] == "sensor.inverter_grid_current_l1"


def test_a_generic_derived_candidate_is_low_confidence_and_flags_the_unverified_sign() -> None:
    r = Registry()
    r.device("m", name="Grid meter", entry="x")
    for n in "123":
        r.add("somebrand", f"grid_meter_power_l{n}", f"p_{n}", device_class="power", unit="W", device="m", entry="x")
        r.add("somebrand", f"grid_meter_voltage_l{n}", f"v_{n}", device_class="voltage", unit="V", device="m", entry="x")

    candidate = one_meter(r)

    assert candidate.mode == MEASUREMENT_MODE_DERIVED
    assert candidate.confidence == "low"
    assert WARNING_SIGN_UNVERIFIED in candidate.warnings
    assert candidate.estimated is True


def test_two_current_families_on_one_device_are_both_offered_not_guessed() -> None:
    r = Registry()
    r.device("m", name="Grid meter", entry="x")
    for n in "123":
        r.add("somebrand", f"grid_meter_current_l{n}", f"a_{n}", device_class="current", unit="A", device="m", entry="x")
        r.add("somebrand", f"grid_meter_avg_current_l{n}", f"b_{n}", device_class="current", unit="A", device="m", entry="x")

    detection = r.detect()

    assert len(detection.meters) == 2
    assert all(c.confidence == "low" for c in detection.meters)


def test_a_wrong_unit_excludes_an_entity_from_the_generic_match() -> None:
    r = Registry()
    r.device("m", name="Grid meter", entry="x")
    for n in "123":
        r.add("somebrand", f"grid_meter_current_l{n}", f"a_{n}", device_class="current", unit="W", device="m", entry="x")

    assert r.detect().meters == ()


# ---- batteries ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "integration", "entity_id", "inverted"),
    [
        (huawei_solar, "huawei_solar", "sensor.battery_charge_discharge", False),
        (sigen, "sigen", "sensor.sigen_plant_ess_power", False),
        (victron_gx, "victron_gx", "sensor.gx_battery_power", False),
        (solaredge_modbus_multi, "solaredge_modbus_multi", "sensor.se_b1_dc_power", False),
        (solarman_deye, "solarman", "sensor.deye_battery_power", True),
        (fronius_with_meter, "fronius", "sensor.inverter_power_battery", True),
        (enphase_envoy, "enphase_envoy", "sensor.envoy_battery_discharge", True),
        (lambda: goodwe(large_et=True), "goodwe", "sensor.gw_battery_power", True),
    ],
)
def test_the_battery_sign_convention_comes_with_the_integration(build, integration, entity_id, inverted) -> None:
    detection = build().detect()

    battery = next(b for b in detection.batteries if b.integration == integration)
    assert battery.entity_id == entity_id
    assert battery.inverted is inverted


def test_battery_only_integrations_are_detected_as_batteries() -> None:
    r = Registry()
    r.add("powerwall", "powerwall_battery_power", "PW1_battery_instant_power", device_class="power", unit="kW", entry="pw")
    r.add("teslemetry", "tesla_battery_power", "VIN1-battery_power", device_class="power", unit="kW", entry="tm")

    detection = r.detect()

    assert detection.meters == ()
    assert {b.integration: b.inverted for b in detection.batteries} == {"powerwall": True, "teslemetry": True}


def test_sma_charge_and_discharge_are_a_pair() -> None:
    detection = sma_with_inverter().detect()

    battery = detection.batteries[0]
    assert battery.entity_id == "sensor.sma_battery_charge"
    assert battery.discharge_entity_id == "sensor.sma_battery_discharge"
    data = apply_battery_candidate({}, battery)
    assert data["battery_aggregate_power_entity"] == "sensor.sma_battery_charge"
    assert data["battery_discharge_power_entity"] == "sensor.sma_battery_discharge"
    assert data["battery_power_inverted"] is False


def test_a_disabled_battery_entity_is_offered_for_enabling() -> None:
    r = Registry()
    r.add("goodwe", "gw_battery", "SN-pbattery1", device_class="power", unit="W", entry="gw", disabled=True)

    assert r.detect().batteries[0].disabled_entity_ids == ("sensor.gw_battery",)


# ---- devices with their own load balancing ------------------------------------------------------


def test_devices_that_balance_load_themselves_are_reported() -> None:
    devices = [
        RegistryDevice("eq", model="Equalizer", name="Easee Equalizer", config_entry_ids=("easee_entry",)),
        RegistryDevice("zs", model="Zaptec Sense", name="Sense", config_entry_ids=("zaptec_entry",)),
        RegistryDevice("fa", model="EnergyHub", name="Ferroamp", config_entry_ids=("ferroamp_entry",)),
        RegistryDevice("op", model="P1", name="ONEp1", config_entry_ids=("onep1_entry",)),
        RegistryDevice("ch", model="Home", name="Easee Home", config_entry_ids=("easee_entry",)),  # a charger
        RegistryDevice("sh", model="Pro 3EM", name="Shelly", config_entry_ids=("shelly_entry",)),
    ]
    platforms = {
        "easee_entry": "easee",
        "zaptec_entry": "zaptec",
        "ferroamp_entry": "ferroamp",
        "onep1_entry": "onep1",
        "shelly_entry": "shelly",
    }

    found = find_own_load_balancing(devices, platforms)

    assert [(item.integration, item.device_name) for item in found] == [
        ("easee", "Easee Equalizer"),
        ("zaptec", "Sense"),
        ("ferroamp", "Ferroamp"),
        ("onep1", "ONEp1"),
    ]


# ---- freshness ----------------------------------------------------------------------------------


def test_a_source_slower_than_the_maximum_age_warns_and_names_the_option() -> None:
    warnings = freshness_warnings({"sensor.se_m1_current_a": "solaredge_modbus_multi"}, 120.0)

    assert [(w.code, w.integration, w.interval_s) for w in warnings] == [
        (WARNING_SLOW_UPDATE, "solaredge_modbus_multi", 300.0)
    ]
    assert "Polling" in (warnings[0].option or "")


def test_a_source_faster_than_the_maximum_age_does_not_warn() -> None:
    assert freshness_warnings({"sensor.dsmr_current_l1": "dsmr", "sensor.f": "fronius"}, 120.0) == []


def test_the_same_source_warns_when_the_maximum_age_is_lowered() -> None:
    warnings = freshness_warnings({"sensor.dsmr_current_l1": "dsmr"}, 20.0)

    assert [(w.integration, w.option) for w in warnings] == [("dsmr", "Minimum time between updates")]


def test_an_integration_without_an_interval_option_warns_without_naming_one() -> None:
    warnings = freshness_warnings({"sensor.hw_power": "huawei_solar"}, 10.0)

    assert warnings[0].integration == "huawei_solar" and warnings[0].option is None


def test_each_integration_warns_once() -> None:
    warnings = freshness_warnings({f"sensor.se_{n}": "solaredge_modbus_multi" for n in range(9)}, 60.0)

    assert len(warnings) == 1


def test_a_write_on_change_source_is_flagged_not_treated_as_slow() -> None:
    warnings = freshness_warnings({"sensor.edl": "edl21", "sensor.z": "zha"}, 120.0)

    assert {w.integration for w in warnings} == {"edl21", "zha"}
    assert {w.code for w in warnings} == {WARNING_ON_CHANGE_ONLY}


def test_unknown_integrations_raise_no_warning() -> None:
    assert freshness_warnings({"sensor.x": "unknown_integration"}, 1.0) == []


# ---- applying a candidate -----------------------------------------------------------------------


def test_applying_a_direct_candidate_keeps_the_derived_entities_and_sets_the_flags() -> None:
    candidate = one_meter(tibber_pulse())
    stored = {"measurement_mode": MEASUREMENT_MODE_DERIVED, "derived_entities": {"L1": {"power": "sensor.keep"}}}

    data = apply_meter_candidate(stored, candidate)

    assert data["measurement_mode"] == MEASUREMENT_MODE_DIRECT
    assert data["direct_entities"]["L2"] == "sensor.tibber_current_l2"
    assert data["derived_entities"] == {"L1": {"power": "sensor.keep"}}
    assert data["site_current_source"] is None


def test_applying_a_candidate_with_a_total_stores_it_and_one_without_clears_the_old_one() -> None:
    with_total = one_meter(tibber_pulse_with_production())
    without = one_meter(tibber_pulse())

    data = apply_meter_candidate({}, with_total)
    assert data["grid_power_source"] == {
        "power": "sensor.tibber_power",
        "power_export": "sensor.tibber_power_production",
    }
    assert data["grid_power_inverted"] is False

    # A total left from the previous meter would be read as the new meter's.
    assert "grid_power_source" not in apply_meter_candidate(data, without)
    assert apply_meter_candidate({"grid_power_source": {"power": "sensor.x"}}, with_total)["grid_power_source"] == {
        "power": "sensor.tibber_power",
        "power_export": "sensor.tibber_power_production",
    }


def test_applying_the_easee_candidate_stores_an_attributes_source() -> None:
    data = apply_meter_candidate({}, one_meter(easee_equalizer()))

    assert data["site_current_source"]["kind"] == "attributes"
    assert data["site_current_source"]["attribute_unit_override"] == "A"
    assert data["direct_entities"] == {}


def test_applying_a_huawei_candidate_sets_both_sign_flags() -> None:
    data = apply_meter_candidate({}, one_meter(huawei_solar()))

    assert data["site_current_signed"] is True
    assert data["grid_power_inverted"] is True
    assert data["derived_entities"]["L1"]["current"] == "sensor.meter_current_a"
