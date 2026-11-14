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
* Victron, core `victron_gx` (victron-mqtt 2026.11.1): `<installation>_<device type>_<device id>_<short id>`.
* GoodWe, core `goodwe` (goodwe 0.4.10): `goodwe-<key>-<serial>`, the key in the middle.
* Enphase, core `enphase_envoy`: `<serial>_<key>`.

Beside the facts, an adapter may know two limits (read 2026-11-13; the Sigenergy keys on the owner's plant's registry):

* **The plant's maximum** (`plant_max`): the most the whole plant puts out (Sigenergy `plant_max_active_power`,
  13.2 kW beside a battery rated 23.04 kW): the battery charges and discharges through it, so the plan takes the lower.
* **The reserve** (`reserves`): the SoC the battery itself does not discharge below (its discharge cut-off or backup
  reserve). Several may be read; the highest that can be read counts. Sigenergy's are disabled by default; Huawei's
  need the installer password; GoodWe states the depth of discharge (the floor is 100 less it). SolaX and Victron:
  none mapped yet.

A control is what SpotNav *could* write; nothing in this release writes it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

#: The facts an adapter may know where to read.
FactRole = Literal["soc", "capacity", "rated_charge", "rated_discharge", "firmware"]

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
    #: As `site_detection.BatteryRow.device_manufacturer`: for an integration that speaks for several makers.
    manufacturer: str | None = None
    #: The plant's maximum output (kW), when it can be lower than the battery's rating.
    plant_max: Match | None = None
    #: Where the battery's own floor is kept (see the module).
    reserves: tuple[ReserveSource, ...] = ()


def _key(key: str, *domains: str, reject: str | None = None) -> Match:
    """The key at the end of the unique id, after `_` or at its start."""
    return Match(domains or ("sensor",), re.compile(rf"(?:^|_){re.escape(key)}$"), None if reject is None else re.compile(reject))


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

VICTRON: Final = Adapter(
    brand="victron",
    label="Victron (GX)",
    platforms=("victron_gx",),
    facts={"soc": _key("system_dc_battery_soc")},
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

GOODWE: Final = Adapter(
    brand="goodwe",
    label="GoodWe",
    platforms=("goodwe",),
    facts={"soc": _goodwe("battery_soc")},
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

ADAPTERS: Final[tuple[Adapter, ...]] = (SIGENERGY, HUAWEI, SOLAREDGE, SOLAX, VICTRON, GOODWE, ENPHASE)


def adapter_for(platform: str, manufacturer: str | None = None) -> Adapter | None:
    """The adapter of an integration, `None` for a brand SpotNav has none for."""
    for adapter in ADAPTERS:
        if platform not in adapter.platforms:
            continue
        if adapter.manufacturer is not None and adapter.manufacturer not in (manufacturer or "").lower():
            continue
        return adapter
    return None
