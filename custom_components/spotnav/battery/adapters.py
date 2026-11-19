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
* SolaX Modbus, `solax_modbus` (wills106 `8a1f3fc`): `<hub name>_<key>`, the SolaX plugin's devices only.
* Three more plugins of `solax_modbus` (wills106 `4acd644`, read 2026-11-18), each its own adapter, told apart by
  the manufacturer the plugin gives the device, and read only (no control is mapped for any of them):
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
    (`battery_max_cell_temperature`) is not the average and is not read.
* Victron, core `victron_gx` (victron-mqtt 2026.11.1): `<installation>_<device type>_<device id>_<short id>`; the HACS
  `victron_mqtt` (tomer-w/ha-victron-mqtt 2026.11.2, same short ids) is read the same way.
* Victron Modbus, community `victron` (sfstar/hass-victron `32fb345`): `<unit id>_<register key>`, the GX system at unit
  100, a battery monitor or BMS at 225 by default.
* GoodWe, core `goodwe` (goodwe 0.4.10): `goodwe-<key>-<serial>`, the key in the middle.
* Enphase, core `enphase_envoy`: `<serial>_<key>`.

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
  `inverter_ess_average_cell_temperature`. Read only: nothing plans on it yet.

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
        "capacity": _key("battery_install_capacity"),
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
    facts={"soc": _key("battery_capacity_total")},
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
    facts={"soc": _key("battery_level"), "capacity": _key("max_capacity")},
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

ADAPTERS: Final[tuple[Adapter, ...]] = (
    SIGENERGY, HUAWEI, SOLAREDGE, SOLAX, SOFAR, GROWATT, SOLINTEG, VICTRON, VICTRON_MODBUS, GOODWE, TESLA, ENPHASE,
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
