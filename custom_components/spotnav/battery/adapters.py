"""Per-brand home-battery adapters: where each integration keeps the battery's facts and its controls.

Every entity is found by the integration's domain (the registry's platform) and the key at the fixed place of its
unique id, never by its entity id, which a person may rename. The entities are looked for in the config entry of
the site's battery entity, the battery's own device first.

Sources, read 2026-11-13 (`plans/research_integrations/battery_control_2026-10-03.md` has the background):

* Sigenergy, `sigen` (TypQxQ/Sigenergy-Local-Modbus 1.2.7.5): unique id `<entry>_<device part>_<key>`, the
  plant's keys start `plant_` (a real plant's registry, `ems_work_mode` `ai_mode`, limit 23.04 kW = its rating).
* Huawei Solar, `huawei_solar` (wlcrs `c754df5`, library huawei-solar 3.1.0): `<serial>_<register name>`.
* SolarEdge Modbus Multi, `solaredge_modbus_multi` (WillCodeForCats `0ab6569`): `<model>_<serial>[_B<n>]_<key>`;
  the storage controls sit on the inverter, the battery's facts on its own `B<n>` device.
* SolaX Modbus, `solax_modbus` (wills106 `8a1f3fc`): `<hub name>_<key>`, the SolaX plugin's devices only. The
  firmware is the computed `software_version` (the raw `firmware_dsp`/`firmware_arm` registers are internal); the
  same string is the inverter device's `sw_version`, which `facts.py` falls back on (wills106 `4acd644`). The size
  is `battery_install_capacity` (kWh, GEN3) or `battery_system_installed_capacity` (Wh, GEN4-6, Modbus protocol
  100 and later); both are disabled by default. `bms_battery_capacity` is not read: on GEN5/6 it is battery 1 only.
* Three more plugins of `solax_modbus` (wills106 `4acd644`, read 2026-11-18), each its own adapter, told apart by
  the manufacturer the plugin gives the device, and read only here (Solinteg alone has a control, in `commands.py`):
  * Sofar (`plugin_sofar.py`, "Sofar Solar"): the level is `battery_capacity_total` (%, all battery inputs; the
    per-input `battery_capacity_1..8` are not read). The floor is 100 less `bat_config_depth_of_discharge` (%, a
    sensor that is disabled by default). The size and the ratings are learnt: `bat_config_capacity` (Ah) and the
    `bat_config_maximum_..._current_limit` are the settings of one battery input and not the plant's. The mode is
    the select `charger_use_mode` ("Energy Storage Mode"), free in "Self Use". The inverter's AC output per phase
    exists (`active_power_output_l1..3`, kW, signed) but no source read states its sign, so it is not the plant's
    power per phase (`plant_phase_power`) until someone confirms it on a device.
  * Growatt (`plugin_growatt.py`, "Growatt New Energy"): the level is `battery_soc` on the SPH/SPA family (the
    plugin's GEN3) and on the TL-XH, TL3-XH and WIT hybrids (GEN4). No size and no rating is an entity, so both are
    learnt. The floor is `load_first_battery_minimum_soc` (GEN3, a number and a sensor of the same key) or
    `ems_discharging_stop_soc_on_grid` (GEN4, a number that needs newer firmware and is disabled by default); the
    off-grid stop SoC is not the floor on the grid. The mode is the select `priority` (GEN3), free in "Load First"; GEN4 has no single mode
    entity (nine time slots), so nothing is said of who runs it.
  * Solinteg (`plugin_solinteg.py`; the device's manufacturer is the plugin author's name, "Gabriel C."): the level
    is `battery_soc`, the size `battery_rated_capacity` (Wh, register 32007, only from inverter firmware
    V27.52.3.0). The ratings are the BMS' present limits `battery_charge_limit` and `battery_discharge_limit` (A)
    times `battery_voltage`, an estimate as for GoodWe. The floor is the number `battery_soc_min_ongrid` (register
    52503; the inverter applies it only while its "Battery SOC Protection On Grid" is on, which is not read). The
    mode is the select `working_mode`, free in "General". The cells' warmest temperature
    (`battery_max_cell_temperature`) is not the average and is not read. The plugin has no signed power per phase of
    the inverter ("Inverter Current L1-3", `grid_current_l1..3`, are unsigned currents and "Meter Active Power L1-3"
    the grid's), so there is no `plant_phase_power` and its control per phase (`commands.SOLINTEG_PHASES`) cannot
    run until there is.
* Victron, core `victron_gx` (victron-mqtt 2026.11.1): `<installation>_<device type>_<device id>_<short id>`; the HACS
  `victron_mqtt` (tomer-w/ha-victron-mqtt 2026.11.2, same short ids) is read the same way. The firmware a control
  check is kept per is the GX's Venus OS version (`platform_venus_firmware_installed_version`, read 2026-11-18 in
  victron-mqtt 2026.11.1): the ESS loop and its control points are the GX's.
* Victron Modbus, community `victron` (sfstar/hass-victron `32fb345`): `<unit id>_<register key>`, the GX system at unit
  100, a battery monitor or BMS at 225 by default.
* GoodWe, core `goodwe` (goodwe 0.4.10): `goodwe-<key>-<serial>`, the key in the middle.
* Enphase, core `enphase_envoy`: `<serial>_<key>`.
* FoxESS, `foxess_modbus` (nathanmarlor/foxess_modbus `94252f0`, read 2026-11-20, `plans/v3_foxess.md`): unique id
  `foxess_modbus_[<prefix>_]<key>`, read only (no control is mapped). One integration for the H1 (G1, G2, LAN), AC1,
  KH, H3, AC3, AIO-H3, H3-Pro and H3-Smart families; the 1KOMMA5 `1K5-HI-<kW>-V1` is read as an H3-Smart. The
  level is `battery_soc` on H1, KH and H3; the H3-Pro and H3-Smart (and 1K5) have no total, only `battery_soc_1` and
  `battery_soc_2` per battery input, which are combined (`soc_packs`, weighted by each pack's `bms_kwh_remaining`;
  a site with one pack has `_2` unknown). No size is an entity anywhere (`bms_kwh_remaining` is the energy left now),
  so it is learnt. The ratings exist only on H1 and KH: `bms_charge_rate` and `bms_discharge_rate` (A, the BMS's
  allowed currents, both disabled by default) times the inverter's battery voltage `invbatvolt`, an estimate as for
  GoodWe; elsewhere they are learnt. The floor is `min_soc_on_grid` (a number and a sensor of the same key); `min_soc`
  is the off-grid floor and is not read. The mode is the select `work_mode`, free in "Self Use" ("Force Charge" and
  "Force Discharge" appear there while the integration's remote control runs); the H1 LAN and early AIO-H3 have no
  mode entity, so it is not required. The firmware is `master_version`.
* Ferroamp, `ferroamp` (henricm/ha-ferroamp `b18097f`, 1.18.0, read 2026-11-18): `<slug>_ehub-<key>` on the EnergyHub
  and `<slug>_ehub-<key>-L1..L3` per phase (`sensor.py` `KeyedFerroampSensor`, `SinglePhaseFerroampSensor`); the
  ESOs and ESMs carry `<slug>_eso_<id>-<key>` and are not read. `soc` (%) and `ratedcap` (Wh) are the whole system's
  and exist only when the hub sends them. `pinv-L1..L3` is the hub's inverter power per phase on the grid's sign
  (import positive, so negative while it delivers): `plant_phase_sign` -1. Nothing is disabled by default.
* Solis, `solis_modbus` (Pho3niX90/solis_modbus v4.5.0, `f92ead8`, read 2026-11-22, `plans/v3_solis_modbus_adapter.md`;
  line numbers at that tag): read only (no control is mapped). Unique ids are `solis_modbus_<serial>_<unique>`
  (`helpers.py` `unique_id_generator`, lines 48-74; the host or the identification stands in before the serial is
  known), a sensor's `<unique>` being its `"unique"` in `sensor_data/hybrid_sensors.py`, e.g.
  `solis_modbus_inverter_battery_soc`; an editable register is both a sensor and a number of the same unique id
  (`sensor.py` lines 29-31, `number.py` lines 26-28). A select is `solis_modbus_<serial>_<register>_select`
  (`sensors/solis_select_entity.py` line 20). The level is `battery_soc` (register 33139, line 726). The ratings
  are the BMS' present limits `battery_charge_current_limitation_bms` and
  `battery_discharge_current_limitation_bms` (33143/33144, A, lines 766/776) times the BMS' voltage
  `battery_voltage_bms` (33141, V, line 745), an estimate as for GoodWe; the integration notes the limit derates with
  the SoC (#467, beside 43012), so the estimate reads low near full or empty. No size is read: the only one is the
  number `battery_rated_capacity` (43019, Ah, line 2202), a battery setting with a default of 200 Ah that no source
  ties to the BMS's own bank, so the size is learnt. The floor is `overdischarge_soc` ("Overdischarge SOC", 43011,
  line 2103, the on-grid one; `overdischarge_offgrid_soc` 43137 is off-grid and not read; `force_charge_soc` is
  where the inverter charges from the grid, not a floor; `backup_soc` holds only in the Reserve/Backup mode and is
  not read). The mode is the select "Storage Mode" (register 43110, `sensor_data/select_sensors.py` lines 65-84),
  free only in "Self-Use": its TOU and Reserve/Backup variants, Feed-in Priority, Off-Grid and Peak Shaving are
  not; and the remote force charge and discharge (register 43135), read from its two switches "RC Force Battery
  Charge" and "RC Force Battery Discharge" (`switch_sensors.py` lines 175-181, unique ids `<serial>_43135_1` and
  `_43135_2`, `helpers.py` `unique_id_generator_binary` lines 77-82), free while both are off. The select of the same
  register (`select_sensors.py` lines 26-40) reads "None" when no force runs, a state SpotNav takes for no reading,
  so it is not used. The selects and switches are left out in the integration's essential-only (read-only) mode,
  so the mode is then unknown. On firmware with the V2 time-of-use (register 33289
  = 0xAA55) the charge and discharge periods are twelve switches (43707, `switch_sensors.py` line 217) beside a
  "Self-Use" mode; they are not read, so "Self-Use" with such periods on still reads free. The firmware is
  `hmi_version` (33002, line 41; the device's `sw_version` is "Protocol <n>", the Modbus protocol, not the
  firmware). The battery's counters are `total_battery_charge_energy` and `total_battery_discharge_energy`
  (33161/33165, kWh in whole kWh, lines 901/932). The battery's power is the derived "Battery Power Net"
  (`battery_power_combined`, discharge-positive, `site_detection.BATTERY_ROWS`). The inverter's "AC Grid Port Active
  Power Phase A-C" (33512/33515/33518, S16, lines 1799-1823) states no sign, so there is no `plant_phase_power`.

Beside the facts, an adapter may know two limits (read 2026-11-13; the Sigenergy keys on the owner's plant's registry):

* **The plant's maximum** (`plant_max`): the most the whole plant puts out (Sigenergy `plant_max_active_power`,
  13.2 kW beside a battery rated 23.04 kW): the battery charges and discharges through it, so the plan takes the lower.
* **The reserve** (`reserves`): the SoC the battery itself does not discharge below (its discharge cut-off or backup
  reserve). Several may be read; the highest that can be read counts. Sigenergy's are disabled by default; Huawei's
  need the installer password; GoodWe and Sofar state the depth of discharge (the floor is 100 less it); Victron's
  is the ESS minimum SoC. SolaX: none mapped yet.

And one live reading (read 2026-11-16):

* **The plant's power per phase** (`plant_phase_power`): the AC power the plant puts out on each of its three phases,
  normalised by `plant_phase_sign` so that positive is output into the house (the sun's power and the battery's
  discharge, both through the hybrid inverter) and negative is drawn (the battery charging). It is the inverter
  side, not the grid point: the house's own current per phase is then the meter's less the chargers' plus this
  (`site/phase_peaks.py`). Sigenergy `plant_phase_{a,b,c}_active_power` (registers 30015/30017/30019, kW, disabled by
  default): the integration states no sign; on the owner's plant (2026-11-16, battery discharging 0.4-1.4 kW, grid
  held at 0 per phase) the three summed to `plant_active_power` (within 0.01 kW on average), which is the sun's power
  less the battery's (`plant_ess_power`, + charging) less ~0.1 kW of the plant's own losses, and phase C's 0.96 kW
  matched the heat pump's own meter on L3. So positive is output. Its `plant_grid_sensor_phase_*` are the grid point
  (the site's meter there) and not this. Phase A, B and C are taken as the meter's L1, L2 and L3.

And one reading kept by the day (read 2026-11-16):

* **The cells' temperature** (`cell_temperature`, the first found of the matches): the battery's average cell
  temperature (°C), whose day the nightly review keeps (`hindsight.cell_temperature`). Sigenergy
  `plant_ess_average_cell_temperature` (plant register 30286 "[ESS] Average cell temperature", firmware V2.8 and later;
  on the owner's plant enabled and named "Husbatteri celltemperatur"), else the inverter's
  `inverter_ess_average_cell_temperature`. Read live too (`facts.BatteryFacts.cell_temperature_c`): the battery's
  preheating (`preheat.py`) and the plan's charge derate read it.

And the battery's own heater (`preheat`, read 2026-11-21, `plans/v3_sigen_preheat.md`): Sigenergy's ESS preheating
(holding registers 50000-50183, protocol V2.9 section 5.2.3, "only Sigen PV M1-HYA/HYB"): the switch
`plant_ess_preheating_enable` (50000), the select `plant_ess_preheating_mode` (50001, Automatic/Manual), the switch
`plant_ess_preheating_advance_enable` (50002, manual mode only), 30 time slots of start, end (epoch seconds) and
target power (kW) as numbers `plant_ess_preheating_tou_<n>_{start_time,end_time,target_power}`, and the number
`plant_ess_preheating_reserved_soc` (50183). All disabled by default in the integration. Read only: SpotNav tells the
person when the switch is off on a cold battery and works out, in shadow, what it would write ahead of a planned
charge (`preheat.py`); it never writes it.

And the battery's lifetime energy counters (`charged_total`, `discharged_total`, read 2026-11-22), from which its
round trip is learnt (`round_trip.py`): the energy that went into the battery and came out of it since it was made.
Each is counted where the brand counts its battery's power, so the round trip learnt is on the same side as the
battery energy the plan measures:

* At the battery (DC), losses in the cells, the BMS and the battery's own converter: Sigenergy
  `plant_accumulated_battery_{charge,discharge}_energy`, Huawei `storage_total_{charge,discharge}`, SolarEdge
  `B<n>_energy_{import,export}` (Wh; the integration lets a battery's counter fall back to 0, a reset here), SolaX and
  Sofar `battery_{input,output}_energy_total` (SolaX's Gen2 registers are marked "need revisit" in the plugin; a wrong
  pair falls outside the believable round trip), Solinteg `battery_{charge,discharge}_total`, GoodWe (ET family)
  `e_bat_{charge,discharge}_total`, FoxESS `battery_{charge,discharge}_total` (the H1 LAN's is the integration's own
  sum of the battery's power), Victron `battery_{charged,discharged}_energy` and the Modbus
  `battery_history_{charged,discharged}energy` (a battery monitor's history; the Modbus registers wrap at 6553.5 kWh).
  Energy from DC-coupled panels into the battery is counted as charged, as it must be: it went into the cells.
* On the AC side, inverter losses included: Enphase `lifetime_battery_{charged,discharged}` (the storage CT).
* Not used: Growatt's `total_ac_charge` counts only what was charged from the grid (not from the panels), beside a
  discharge counter of everything, so its ratio is no round trip; Tesla and Ferroamp have none mapped.

A battery whose integration states its size in amp-hours and its limits as currents (Victron, GoodWe) gets kWh and
kW from the battery voltage the same integration reads (`voltage`): energy = Ah x V, power = A x V. That is an
estimate (the voltage is the live one, not the nominal) and is named as one (`BatteryFacts.estimates`).

A control here is read only: the outside-control watch and the diagnostics read it. What SpotNav would write is the
executor's brand command (`commands.py`), the one write route; nothing in this release writes either.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

#: The facts an adapter may know where to read.
FactRole = Literal[
    "soc",
    "capacity",
    "rated_charge",
    "rated_discharge",
    "firmware",
    # Read in amp-hours, amperes and volts, and turned into kWh and kW with the voltage (see the module).
    "capacity_ah",
    "charge_current",
    "discharge_current",
    "voltage",
    # The battery's lifetime energy counters (kWh, Wh or MWh), for its round trip (`round_trip.py`; see the module).
    "charged_total",
    "discharged_total",
]

#: How a control would be written.
ControlMethod = Literal["number", "select", "switch", "service"]


@dataclass(frozen=True, slots=True)
class Match:
    """An entity of one of `domains` whose lower-cased unique id `pattern` finds and `reject` does not."""

    domains: tuple[str, ...]
    pattern: re.Pattern[str]
    reject: re.Pattern[str] | None = None

    def matches(self, domain: str, unique_id: str) -> bool:
        text = unique_id.lower()
        if domain not in self.domains or not self.pattern.search(text):
            return False
        return self.reject is None or not self.reject.search(text)


@dataclass(frozen=True, slots=True)
class ModeRule:
    """A setting that says who runs the battery: SpotNav may act only while it reads one of `free`."""

    match: Match
    free: frozenset[str]
    label: str
    #: A battery without this entity is not run by it (an optional add-on such as Victron's Dynamic ESS).
    required: bool = True


@dataclass(frozen=True, slots=True)
class ControlSpec:
    """What SpotNav could write for one action, and how the value it finds there is read back."""

    method: ControlMethod
    #: The value written for the action (0 kW for "no discharge", an option, on).
    value: float | str | bool
    match: Match | None = None
    service: str | None = None
    #: A number at or above this (kW) holds no limit: the register's "never set" (Sigenergy's 0xFFFFFFFF).
    unset_at_or_above_kw: float | None = None
    #: States that mean "no limit" (Victron's -1).
    unset_states: frozenset[str] = frozenset()
    #: `unknown` is the "never set" register, not a missing reading (Sigenergy 2.0.1 shows the all-ones value so):
    #: the control is readable, holds no limit, and has no number (never 0).
    unknown_is_unset: bool = False
    #: Writing it changes the battery's own operating mode, which a restore may not bring back.
    changes_mode: bool = False
    note: str = ""


@dataclass(frozen=True, slots=True)
class ReserveSource:
    """An entity that holds the battery's own floor (%)."""

    match: Match
    #: It holds the depth of discharge: the floor is 100 less it (GoodWe).
    depth: bool = False


@dataclass(frozen=True, slots=True)
class SocPack:
    """One battery pack's level (%) and, to weigh it by, the energy it holds now (kWh)."""

    soc: Match
    remaining: Match | None = None


@dataclass(frozen=True, slots=True)
class PreheatSpec:
    """Where a battery's own heater is switched and scheduled (see the module)."""

    enable: Match
    mode: Match | None = None
    advance: Match | None = None
    reserved_soc: Match | None = None
    #: The key of a time slot's field, `{n}` its number from 1 (`slot_key`).
    slot_keys: tuple[str, str, str] | None = None
    slots: int = 0
    #: The option of `mode` that hands the schedule to an outside EMS.
    manual_option: str | None = None

    def slot(self, number: int, field_index: int) -> Match | None:
        """Slot `number`'s start (0), end (1) or target power (2) number entity."""
        if self.slot_keys is None or not 1 <= number <= self.slots:
            return None
        return _key(self.slot_keys[field_index].format(n=number), "number")


@dataclass(frozen=True, slots=True)
class Adapter:
    brand: str
    label: str
    platforms: tuple[str, ...]
    facts: Mapping[FactRole, Match] = field(default_factory=dict)
    modes: tuple[ModeRule, ...] = ()
    #: The plant's grid connection and the state that means "on the grid".
    grid_status: tuple[Match, str] | None = None
    hold: ControlSpec | None = None
    grid_charge: ControlSpec | None = None
    #: As `site_detection.BatteryRow.device_manufacturer`: for an integration that speaks for several makers. A
    #: lower-case part of the device's manufacturer, or several of which one is enough.
    manufacturer: str | tuple[str, ...] | None = None
    #: The plant's maximum output (kW), when it can be lower than the battery's rating.
    plant_max: Match | None = None
    #: Where the battery's own floor is kept (see the module).
    reserves: tuple[ReserveSource, ...] = ()
    #: The plant's AC power on phases A, B and C, and the factor that makes it positive for output (see the module).
    plant_phase_power: tuple[Match, Match, Match] | None = None
    plant_phase_sign: float = 1.0
    #: Where the battery's average cell temperature is read, the first found (see the module).
    cell_temperature: tuple[Match, ...] = ()
    #: A battery that states no level of its own but one per pack (FoxESS H3-Pro, H3-Smart): read only when the
    #: `soc` fact reads none, and combined by `facts.packs_soc`.
    soc_packs: tuple[SocPack, ...] = ()
    #: The battery's own heater, where the integration has it (see the module).
    preheat: PreheatSpec | None = None


def _key(key: str, *domains: str, reject: str | None = None) -> Match:
    """The key at the end of the unique id, after `_` or at its start."""
    return Match(domains or ("sensor",), re.compile(rf"(?:^|_){re.escape(key)}$"), None if reject is None else re.compile(reject))


def _tesla(key: str, *domains: str) -> Match:
    """Tesla Fleet and Teslemetry put the energy site's id before the key: `<energy site id>-<key>`."""
    return Match(domains or ("sensor",), re.compile(rf"^[^-]+-{re.escape(key)}$"))


def _goodwe(key: str, *domains: str) -> Match:
    """GoodWe puts the serial after the key: `goodwe-<key>-<serial>`."""
    return Match(domains or ("sensor",), re.compile(rf"^goodwe-{re.escape(key)}-[^-]+$"))


SIGENERGY: Final = Adapter(
    brand="sigenergy",
    label="Sigenergy",
    platforms=("sigen",),
    facts={
        "soc": _key("plant_ess_soc"),
        "capacity": _key("plant_ess_rated_energy_capacity"),
        "rated_charge": _key("plant_ess_rated_charging_power"),
        "rated_discharge": _key("plant_ess_rated_discharging_power"),
        "firmware": _key("inverter_machine_firmware_version"),
        # The plant's ESS counters (registers 30200/30204, kWh), counted at the battery like `plant_ess_power`.
        "charged_total": _key("plant_accumulated_battery_charge_energy"),
        "discharged_total": _key("plant_accumulated_battery_discharge_energy"),
    },
    modes=(
        ModeRule(_key("plant_ems_work_mode"), frozenset({"max_self_consumption"}), "EMS work mode"),
    ),
    grid_status=(_key("plant_on_off_grid_status"), "on_grid"),
    plant_max=_key("plant_max_active_power"),
    plant_phase_power=(
        _key("plant_phase_a_active_power"), _key("plant_phase_b_active_power"), _key("plant_phase_c_active_power"),
    ),
    cell_temperature=(_key("plant_ess_average_cell_temperature"), _key("inverter_ess_average_cell_temperature")),
    preheat=PreheatSpec(
        enable=_key("plant_ess_preheating_enable", "switch"),
        mode=_key("plant_ess_preheating_mode", "select"),
        advance=_key("plant_ess_preheating_advance_enable", "switch"),
        reserved_soc=_key("plant_ess_preheating_reserved_soc", "number"),
        slot_keys=(
            "plant_ess_preheating_tou_{n}_start_time",
            "plant_ess_preheating_tou_{n}_end_time",
            "plant_ess_preheating_tou_{n}_target_power",
        ),
        slots=30,
        manual_option="Manual",
    ),
    reserves=(
        ReserveSource(_key("plant_ess_discharge_cut_off_soc")),
        ReserveSource(_key("plant_discharge_cut_off_soc", "number")),
        ReserveSource(_key("plant_backup_soc", "number")),
    ),
    hold=ControlSpec(
        "number",
        0.0,
        _key("plant_ess_max_discharging_limit", "number"),
        unset_at_or_above_kw=4_000_000.0,
        unknown_is_unset=True,
        note="Register 40034, disabled by default; read-only mode drops a write silently. Firmware SPC117 is"
        " reported to obey it only with Remote EMS on.",
    ),
    grid_charge=ControlSpec(
        "select",
        "Command Charging (Grid First)",
        _key("plant_remote_ems_control_mode", "select"),
        changes_mode=True,
        note="Needs Remote EMS on, which replaces the app's own mode; turning it off lands in Maximum Self"
        " Consumption, not the mode before.",
    ),
)

HUAWEI: Final = Adapter(
    brand="huawei",
    label="Huawei Solar",
    platforms=("huawei_solar",),
    facts={
        "soc": _key("storage_state_of_capacity"),
        "capacity": _key("storage_rated_capacity"),
        "rated_charge": _key("storage_maximum_charge_power"),
        "rated_discharge": _key("storage_maximum_discharge_power"),
        "charged_total": _key("storage_total_charge"),
        "discharged_total": _key("storage_total_discharge"),
    },
    modes=(
        ModeRule(
            _key("storage_working_mode_settings", "select"),
            frozenset({"maximise_self_consumption"}),
            "working mode",
        ),
    ),
    reserves=(
        ReserveSource(_key("storage_discharging_cutoff_capacity", "number")),
        ReserveSource(_key("storage_backup_power_state_of_charge", "number")),
    ),
    hold=ControlSpec(
        "number",
        0.0,
        _key("storage_maximum_discharging_power", "number"),
        note="Only with the installer password (elevated permissions) in the integration.",
    ),
    grid_charge=ControlSpec(
        "service",
        "forcible_charge",
        service="huawei_solar.forcible_charge",
        note="A forcible charge for a duration (default 60 min); `stop_forcible_charge` ends it.",
    ),
)

SOLAREDGE: Final = Adapter(
    brand="solaredge",
    label="SolarEdge Modbus Multi",
    platforms=("solaredge_modbus_multi",),
    facts={
        "soc": _key("battery_soe"),
        "capacity": _key("max_energy"),
        "rated_charge": _key("max_charge_power"),
        "rated_discharge": _key("max_discharge_power"),
        # The battery's own `B<n>` counters (Wh): import is what went in, export what came out.
        "charged_total": Match(("sensor",), re.compile(r"_b\d+_energy_import$")),
        "discharged_total": Match(("sensor",), re.compile(r"_b\d+_energy_export$")),
    },
    modes=(
        ModeRule(_key("storage_control_mode", "select"), frozenset({"Remote Control"}), "storage control mode"),
    ),
    reserves=(ReserveSource(_key("storage_backup_reserve", "number")),),
    hold=ControlSpec(
        "number",
        0.0,
        _key("storage_discharge_limit", "number"),
        note="Available only in Remote Control; the command mode falls back after its timeout.",
    ),
    grid_charge=ControlSpec(
        "select",
        "Charge from Solar Power and Grid",
        _key("storage_command_mode", "select"),
        note="Needs an AC charge policy that allows grid charging; ends after the command timeout.",
    ),
)

SOLAX: Final = Adapter(
    brand="solax",
    label="SolaX Modbus",
    platforms=("solax_modbus",),
    manufacturer="solax",
    facts={
        "soc": _key("battery_capacity", reject=r"(?:bms|pm|pm_i\d+)_battery_capacity$"),
        # GEN3's and GEN4-6's, never both on one inverter.
        "capacity": Match(("sensor",), re.compile(r"(?:^|_)(?:battery_install_capacity|battery_system_installed_capacity)$")),
        "firmware": _key("software_version"),
        # The BMS' allowed charge and discharge currents (`plugin_solax.py`, input registers 0x24/0x25, A, disabled by
        # default) times the battery voltage (input register 0x14, `battery_voltage_charge`, GEN5/6 `battery_1_...`).
        # GEN5/6 name them for battery 1 only, so a two-battery plant reads low: a floor, never above what it can do.
        # (The holding registers of the same 0x24/0x25 are the person's own "Battery Charge Max Current" settings, the
        # numbers `battery_charge_max_current`; they are not read: they default to 20 A and say nothing of the BMS.)
        "charge_current": _key("bms_charge_max_current"),
        "discharge_current": _key("bms_discharge_max_current"),
        "voltage": Match(
            ("sensor",), re.compile(r"(?:^|_)battery_(?:1_)?voltage_charge$"), re.compile(r"(?:^|_)(?:bms|pm)_")
        ),
        "charged_total": _key("battery_input_energy_total"),
        "discharged_total": _key("battery_output_energy_total"),
    },
    modes=(
        ModeRule(_key("charger_use_mode", "select"), frozenset({"Self Use Mode"}), "use mode"),
        ModeRule(_key("remotecontrol_power_control", "select"), frozenset({"Disabled"}), "remote control"),
    ),
    hold=ControlSpec(
        "select",
        "Enabled No Discharge",
        _key("remotecontrol_power_control", "select"),
        note="Remote control expires; the integration repeats it while its autorepeat duration runs.",
    ),
    grid_charge=ControlSpec(
        "select",
        "Enabled Battery Control",
        _key("remotecontrol_power_control", "select"),
        note="With the remote control's active power as the charge power; expires as above.",
    ),
)

SOFAR: Final = Adapter(
    brand="sofar",
    label="Sofar (SolaX Modbus)",
    platforms=("solax_modbus",),
    manufacturer="sofar",
    facts={
        "soc": _key("battery_capacity_total"),
        "charged_total": _key("battery_input_energy_total"),
        "discharged_total": _key("battery_output_energy_total"),
    },
    modes=(ModeRule(_key("charger_use_mode", "select"), frozenset({"Self Use"}), "energy storage mode"),),
    reserves=(ReserveSource(_key("bat_config_depth_of_discharge"), depth=True),),
)

GROWATT: Final = Adapter(
    brand="growatt",
    label="Growatt (SolaX Modbus)",
    platforms=("solax_modbus",),
    manufacturer="growatt",
    facts={"soc": _key("battery_soc")},
    modes=(ModeRule(_key("priority", "select"), frozenset({"Load First"}), "priority", required=False),),
    reserves=(
        ReserveSource(_key("load_first_battery_minimum_soc", "number", "sensor")),
        ReserveSource(_key("ems_discharging_stop_soc_on_grid", "number")),
    ),
)

SOLINTEG: Final = Adapter(
    brand="solinteg",
    label="Solinteg (SolaX Modbus)",
    platforms=("solax_modbus",),
    manufacturer=("solinteg", "gabriel c."),
    facts={
        "soc": _key("battery_soc"),
        "capacity": _key("battery_rated_capacity"),
        "charge_current": _key("battery_charge_limit"),
        "discharge_current": _key("battery_discharge_limit"),
        "voltage": _key("battery_voltage"),
        "firmware": _key("software_version"),
        "charged_total": _key("battery_charge_total"),
        "discharged_total": _key("battery_discharge_total"),
    },
    modes=(ModeRule(_key("working_mode", "select"), frozenset({"General"}), "working mode"),),
    reserves=(ReserveSource(_key("battery_soc_min_ongrid", "number")),),
)

VICTRON: Final = Adapter(
    brand="victron",
    label="Victron (GX)",
    platforms=("victron_gx", "victron_mqtt"),
    facts={
        "soc": _key("system_dc_battery_soc"),
        # The BMS' own figures (a CAN-bus battery or Lynx NG; "not supported by all Victron products"), in Ah and A.
        "capacity_ah": _key("battery_installed_capacity"),
        "charge_current": _key("battery_max_charge_current"),
        "discharge_current": _key("battery_max_discharge_current"),
        "voltage": _key("system_dc_battery_voltage"),
        # The GX's Venus OS version (`/Firmware/Installed/Version` on the platform service): its ESS loop and the
        # control points are the GX's, so a change of it is what a control check is kept per.
        "firmware": _key("platform_venus_firmware_installed_version"),
        # The battery monitor's history (`/History/ChargedEnergy`, `/History/DischargedEnergy`, kWh), where it has one.
        "charged_total": _key("battery_charged_energy"),
        "discharged_total": _key("battery_discharged_energy"),
    },
    # The ESS minimum SoC (Settings/CGwacs/BatteryLife/MinimumSocLimit), the floor the system discharges to.
    reserves=(ReserveSource(_key("system_ess_min_soc_limit", "number")),),
    modes=(
        ModeRule(
            _key("system_ess_batterylife_state", "select", "sensor"),
            frozenset({"optimized_battery_life", "optimized_no_battery_life"}),
            "ESS mode",
        ),
        ModeRule(_key("system_settings_dess_mode", "select", "sensor"), frozenset({"off"}), "Dynamic ESS", required=False),
    ),
    hold=ControlSpec(
        "number",
        0.0,
        _key("system_ess_max_inverter_power_limit", "number"),
        unset_states=frozenset({"-1", "-1.0"}),
        note="/Settings/CGwacs/MaxDischargePower; -1 is no limit. A setting stored on the GX: write rarely.",
    ),
)

#: The community Modbus integration (sfstar/hass-victron): the same system, read through the GX's Modbus-TCP
#: registers. Its battery capacity register (309) is in Ah but the entity carries the unit A; the GX's Dynamic ESS
#: setting "battery capacity" (register 5420) is a sensor in kWh and is preferred where the person has filled it in.
#: The ESS mode and Dynamic ESS selects exist only with the integration's advanced options, so without them the
#: mode is `unknown` and SpotNav leaves the battery alone.
VICTRON_MODBUS: Final = Adapter(
    brand="victron_modbus",
    label="Victron (Modbus)",
    platforms=("victron",),
    facts={
        "soc": _key("system_battery_soc"),
        "capacity": _key("settings_dynamicess_batterycapacity"),
        "capacity_ah": _key("battery_capacity"),
        "charge_current": _key("battery_info_maxchargecurrent"),
        "discharge_current": _key("battery_info_maxdischargecurrent"),
        "voltage": _key("system_battery_voltage"),
        # The battery monitor's history registers 302/301 (kWh in steps of 0.1, which wrap at 6553.5).
        "charged_total": _key("battery_history_chargedenergy"),
        "discharged_total": _key("battery_history_dischargedenergy"),
    },
    reserves=(ReserveSource(_key("settings_ess_batterylife_minimumsoc", "number")),),
    modes=(
        ModeRule(
            _key("settings_ess_mode", "select"),
            frozenset({"SELF_CONSUMPTION_WITH_BATTERY_LIFE", "SELF_CONSUMPTION"}),
            "ESS mode",
        ),
        ModeRule(_key("settings_dynamicess_mode", "select"), frozenset({"OFF"}), "Dynamic ESS", required=False),
    ),
)

GOODWE: Final = Adapter(
    brand="goodwe",
    label="GoodWe",
    platforms=("goodwe",),
    facts={
        "soc": _goodwe("battery_soc"),
        # The BMS' present limits (A) and the battery voltage: the inverter's own limits, no size is exposed in core
        # (the library's `battery_capacity` is a setting that core does not publish).
        "charge_current": _goodwe("battery_charge_limit"),
        "discharge_current": _goodwe("battery_discharge_limit"),
        "voltage": _goodwe("vbattery1"),
        # The ET family's battery counters (registers 35206/35209, kWh).
        "charged_total": _goodwe("e_bat_charge_total"),
        "discharged_total": _goodwe("e_bat_discharge_total"),
    },
    modes=(ModeRule(_goodwe("operation_mode", "select"), frozenset({"general"}), "operating mode"),),
    reserves=(ReserveSource(_goodwe("battery_discharge_depth", "number"), depth=True),),
    # No discharge cap: the depth-of-discharge trick has the cut-off-above-SoC grid-charge risk.
    grid_charge=ControlSpec(
        "select",
        "eco_charge",
        _goodwe("operation_mode", "select"),
        changes_mode=True,
        note="Eco charge is an operating mode; its power and SoC are not entities in core.",
    ),
)

#: Tesla Powerwall through the cloud integrations of Home Assistant core, `tesla_fleet` and `teslemetry` (core
#: `3df9333`, read 2026-11-18; `plans/research_integrations/goodwe_tesla_enphase_control_2026-11.md`): an energy
#: site's unique id is `<energy site id>-<key>`. The level is `percentage_charged`, the size `total_pack_energy` (Wh,
#: disabled by default), the grid state `island_status`. No rated power is an entity. The floor is the backup reserve
#: (number `backup_reserve_percent`, written by the control: while SpotNav holds or charges, the floor reads what it
#: wrote). The mode (select `default_real_mode`) is free only in `self_consumption`: `backup` pins the reserve and
#: `autonomous` (Time-Based Control) is the person's own optimiser.
TESLA: Final = Adapter(
    brand="tesla",
    label="Tesla Powerwall",
    platforms=("tesla_fleet", "teslemetry"),
    facts={"soc": _tesla("percentage_charged"), "capacity": _tesla("total_pack_energy")},
    modes=(ModeRule(_tesla("default_real_mode", "select"), frozenset({"self_consumption"}), "operation mode"),),
    grid_status=(_tesla("island_status"), "on_grid"),
    reserves=(ReserveSource(_tesla("backup_reserve_percent", "number")),),
)

ENPHASE: Final = Adapter(
    brand="enphase",
    label="Enphase",
    platforms=("enphase_envoy",),
    facts={
        "soc": _key("battery_level"),
        "capacity": _key("max_capacity"),
        # The storage CT's counters (Wh): on the AC side, where the battery's power is read too.
        "charged_total": _key("lifetime_battery_charged"),
        "discharged_total": _key("lifetime_battery_discharged"),
    },
    modes=(ModeRule(_key("storage_mode", "select"), frozenset({"self_consumption"}), "storage mode"),),
    reserves=(ReserveSource(_key("reserve_soc", "number")),),
    # No discharge cap: only "Full backup" or a reserve at the current level, both coarse.
    grid_charge=ControlSpec(
        "switch",
        True,
        _key("charge_from_grid", "switch"),
        note="Takes effect through the Envoy's tariff settings and may lag or be overwritten from Enlighten.",
    ),
)

FOXESS: Final = Adapter(
    brand="foxess",
    label="FoxESS",
    platforms=("foxess_modbus",),
    facts={
        "soc": _key("battery_soc"),
        "charge_current": _key("bms_charge_rate"),
        "discharge_current": _key("bms_discharge_rate"),
        "voltage": _key("invbatvolt"),
        "firmware": _key("master_version"),
        "charged_total": _key("battery_charge_total"),
        "discharged_total": _key("battery_discharge_total"),
    },
    soc_packs=(
        SocPack(_key("battery_soc_1"), _key("bms_kwh_remaining_1")),
        SocPack(_key("battery_soc_2"), _key("bms_kwh_remaining_2")),
    ),
    modes=(ModeRule(_key("work_mode", "select"), frozenset({"Self Use"}), "work mode", required=False),),
    reserves=(ReserveSource(_key("min_soc_on_grid", "number", "sensor")),),
)


def _ehub(key: str, *domains: str) -> Match:
    """Ferroamp's EnergyHub entity: `<slug>_ehub-<key>` (the ESOs' and ESMs' `<slug>_eso_<id>-<key>` never match)."""
    return Match(domains or ("sensor",), re.compile(rf"_ehub-{re.escape(key)}$"))


FERROAMP: Final = Adapter(
    brand="ferroamp",
    label="Ferroamp EnergyHub",
    platforms=("ferroamp",),
    facts={"soc": _ehub("soc"), "capacity": _ehub("ratedcap")},
    # The hub's inverter power per phase, import-positive like its grid power (`pext - pinv` is each phase's load).
    plant_phase_power=(_ehub("pinv-l1"), _ehub("pinv-l2"), _ehub("pinv-l3")),
    plant_phase_sign=-1.0,
    # No mode entity: the hub's own control (FerroAI, the person's EnergyCloud settings) is not readable here.
)

#: Solis through `solis_modbus` (see the module): read only.
SOLIS: Final = Adapter(
    brand="solis",
    label="Solis",
    platforms=("solis_modbus",),
    facts={
        "soc": _key("solis_modbus_inverter_battery_soc"),
        "charge_current": _key("solis_modbus_inverter_battery_charge_current_limitation_bms"),
        "discharge_current": _key("solis_modbus_inverter_battery_discharge_current_limitation_bms"),
        "voltage": _key("solis_modbus_inverter_battery_voltage_bms"),
        "firmware": _key("solis_modbus_inverter_hmi_version"),
        "charged_total": _key("solis_modbus_inverter_total_battery_charge_energy"),
        "discharged_total": _key("solis_modbus_inverter_total_battery_discharge_energy"),
    },
    modes=(
        ModeRule(_key("43110_select", "select"), frozenset({"Self-Use"}), "storage mode"),
        ModeRule(_key("43135_1", "switch"), frozenset({"off"}), "remote force charge", required=False),
        ModeRule(_key("43135_2", "switch"), frozenset({"off"}), "remote force discharge", required=False),
    ),
    reserves=(ReserveSource(_key("solis_modbus_inverter_overdischarge_soc", "number", "sensor")),),
)

ADAPTERS: Final[tuple[Adapter, ...]] = (
    SIGENERGY, HUAWEI, SOLAREDGE, SOLAX, SOFAR, GROWATT, SOLINTEG, VICTRON, VICTRON_MODBUS, GOODWE, TESLA, ENPHASE,
    FERROAMP, FOXESS, SOLIS,
)


def adapter_for(platform: str, manufacturer: str | None = None) -> Adapter | None:
    """The adapter of an integration, `None` for a brand SpotNav has none for."""
    for adapter in ADAPTERS:
        if platform not in adapter.platforms:
            continue
        wanted = (adapter.manufacturer,) if isinstance(adapter.manufacturer, str) else adapter.manufacturer
        if wanted is not None and not any(part in (manufacturer or "").lower() for part in wanted):
            continue
        return adapter
    return None
