"""What SpotNav would write to a home battery for each of the plan's actions, per brand: built, not reachable.

Each brand's control is a set of **roles** (the entities it writes or reads back) and, per plan action, the
**state** those roles must be in: a value per role, or `SNAPSHOT` for "the value found before SpotNav first wrote".
"Normal" (the plan's `self_consume`, and every restore) is the state with every written role back at its snapshot.
The executor (`executor.py`) writes only the roles whose reading differs from the wanted state, one at a time and in
the brand's order, each confirmed by read-back through the ledger before the next.

The entity keys, options and services below were checked against each integration's source on 2026-11-13
(shallow clones; `plans/research_integrations/battery_control_2026-10-03.md` has the background):

* **Sigenergy**, `sigen`, TypQxQ/Sigenergy-Local-Modbus `94e38daef7e88bbd8842b5bdb9712adf735ab0fc` (2.0.1):
  switch `plant_remote_ems_enable` (register 40029, `switch.py`), select `plant_remote_ems_control_mode` (40031,
  `select.py`, available only while 40029 is on), numbers `plant_ess_max_charging_limit` /
  `plant_ess_max_discharging_limit` (40032/40034, kW, 0-100, step 0.001, `number.py`). Every one is disabled by
  default; the integration is read-only by default and then drops a write with a log line and no error
  (`modbus.py` `async_write_parameter`), which is why every write is read back. A limit that holds the register's
  all-ones "never set" value now reads `unknown` (`number.py` `_power_limit_value`); it is restored as the rating
  (`unknown_is_unset`), never written back as `unknown`, and an `unknown` read-back is never a confirmed write.
  Remote EMS: limits first, then the switch, then the control mode; back: the control mode to Maximum Self
  Consumption (so 40031 never keeps a command for the next one who turns Remote EMS on), the switch off, the limits
  back. Only a plant in Maximum Self Consumption is ever taken (`adapters.SIGENERGY`'s mode rule), and turning
  Remote EMS off lands there (research §2.2), so the hand-back is clean. The discharge cut-off SoC is never
  written: with it above the SoC the plant grid-charges to reach it (research §3.1 D).
* **SolarEdge Modbus Multi**, `solaredge_modbus_multi`, WillCodeForCats `0ab6569f1e20bb5abf735e5b5edf253cd8766cfd`:
  only while the storage control mode is already Remote Control (`select.py` `StorageControlMode`, option 4): the
  command mode (`storage_command_mode`, `const.py` `STORAGE_MODE`), the charge and discharge limits (W) and the
  command timeout (s, at most 86400), all available only in Remote Control (`number.py`). The timeout is the
  watchdog: when SpotNav stops renewing the command the inverter falls back to its default mode by itself. A grid
  charge needs an AC charge policy other than Disabled (`ac_charge_policy`).
* **Huawei Solar**, `huawei_solar`, wlcrs `c754df504587cac15f8f9c5866e88013e64c67c4` (library huawei-solar 3.1.0):
  services `forcible_charge` / `forcible_discharge` (`device_id` of the battery, `power` W, `duration` minutes
  1-1440) and `stop_forcible_charge` (`services.py`), registered only with the installer password; read back from
  the "Forcible charge" summary sensor (`<serial>_forcible_charge_discharge_write`, `sensor.py`
  `HuaweiSolarForcibleChargeEntity`: "Stopped", "Charging at <W>W for <min> minutes", ...). The duration is the
  watchdog. Hold and "store nothing" use the numbers `storage_maximum_discharging_power` /
  `storage_maximum_charging_power` (W, `number.py`), as Predbat's Huawei template does.
* **SolaX Modbus**, `solax_modbus`, wills106 `5d734b13a87afb9d5dd59915163aa397d73aa862`: remote control mode 1
  (`plugin_solax.py`): select `remotecontrol_power_control` (Disabled, Enabled Battery Control, Enabled Self Use,
  Enabled No Discharge, ...), number `remotecontrol_active_power` (W, + charge, step 100), number
  `remotecontrol_autorepeat_duration` (s), button `remotecontrol_trigger`. The select and numbers are local to the
  integration (`WRITE_DATA_LOCAL`): their read-back proves only that the integration holds them; the button sends
  them, has no read-back, and the battery's effect is judged by the monitor. The integration repeats the command
  until the autorepeat duration runs out and then writes Disabled: that is the watchdog. Remote control is not
  stored in EEPROM.
* **Export limit** (`EXPORT_CONTROLS`, the action `ACTION_CURTAIL_EXPORT`: no export while the sell price is below
  zero, `export_curtail.py`): its own control beside the battery's, written by the same executor and ledger. Sigenergy:
  number `plant_grid_point_maximum_export_limitation` ("Grid Export Limitation", register 40038, U32, gain 1000, kW
  0-100, step 0.001, disabled by default; TypQxQ/Sigenergy-Local-Modbus `41182ec23b9d146ab5c01379a7f08994aaada75d`
  (2.0.3) `number.py` lines 185-199 (the key at 186), `modbusregisterdefinitions.py` lines 1624-1632). It applies in every EMS mode
  (the integration's `docs/MODBUS_ENTITIES.md`, "Grid export power limit": "All EMS modes", "Hybrid / PV · grid
  sensor"), so it does not need Remote EMS, but it needs the plant's grid sensor. Its never-set all-ones value reads
  `unknown` as the battery limits do; it is restored as the plant's maximum output (`plant_max_active_power`), the most
  the plant can export anyway, and the export control is not started without that figure.
* Victron, GoodWe, Enphase and brands without an adapter: **none**. (Victron's discharge cap is a setting stored on
  the GX; GoodWe's and Enphase's controls in core are whole-mode switches or a reserve trick, see
  `battery_cycle_cost_and_modes_2026-11-13.md` part 3.)

Pure: no Home Assistant import. Nothing here writes; it says what would be written.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from .adapters import HUAWEI, SIGENERGY, SOLAREDGE, SOLAX, Adapter, Match, _key
from .plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
)

#: The value a role had before SpotNav first wrote it.
SNAPSHOT: Final = "<snapshot>"

#: How long a brand's own watchdog runs (s) and when SpotNav renews it: a command SpotNav stops renewing (HA died)
#: falls back by itself within this.
WATCHDOG_S: Final = 1800
WATCHDOG_RENEW_S: Final = 900

RoleMethod = Literal["number", "select", "switch", "button", "service"]
#: The export-limit rule's action (`export_curtail.py`): the site's export limit at 0 kW. Not a battery plan action.
ACTION_CURTAIL_EXPORT: Final = "curtail_export"

#: The plan's actions that write something; `self_consume` is "normal".
CONTROL_ACTIONS: Final = (ACTION_HOLD, ACTION_GRID_CHARGE, ACTION_DISCHARGE_HOUSE, ACTION_EXPORT_DISCHARGE)

_NO_READING: Final = frozenset({"", "unavailable", "unknown", "none"})

Native = float | str | bool


@dataclass(frozen=True, slots=True)
class Role:
    """One entity the control writes (or, for a service, reads back)."""

    name: str
    method: RoleMethod
    match: Match
    #: kW to the entity's unit (1000 for a W entity).
    scale: float = 1.0
    #: The step a value is rounded to before it is written (in the entity's unit).
    step: float | None = None
    #: Whether the value found first is written back on restore.
    restored: bool = True
    #: For a service role: `(write, service)` pairs by the kind of value wanted (`charge`, `discharge`, `stop`).
    services: Mapping[str, str] = field(default_factory=dict)
    #: A read that is the register's "never set" (restored as the rating): its kind (`charge`, `discharge`).
    unset_as_rating: str | None = None
    #: Read as unset when `unknown` (Sigenergy 2.0.1 shows the all-ones value as `unknown`).
    unknown_is_unset: bool = False
    #: Turns the read into the value compared (Huawei's summary text).
    normalize: Callable[[str | None], str | None] | None = None
    #: Readings that mean the role is not in use (a select unavailable while its switch is off): no snapshot needed.
    idle_when: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Want:
    """The state a role must be in for an action: a native value, or `SNAPSHOT`."""

    role: str
    value: Native


@dataclass(frozen=True, slots=True)
class BrandControl:
    brand: str
    adapter: Adapter
    roles: tuple[Role, ...]
    #: Per action, the wanted state of every role it sets (others: their snapshot).
    states: Mapping[str, Callable[[float, int], tuple[Want, ...]]]
    #: The order roles are written when an action is entered or changed, and when the battery is handed back.
    enter_order: tuple[str, ...]
    restore_order: tuple[str, ...]
    #: Mode-rule labels and the states SpotNav's own commands leave them in (a plant in Remote EMS that SpotNav
    #: turned on is not "managed by its own system").
    own_states: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: The roles renewed to keep a watchdog alive (`WATCHDOG_RENEW_S`); empty when the brand has none.
    watchdog_roles: tuple[str, ...] = ()
    #: Roles that must read a value before anything is written (`(role, allowed states, reason)`).
    requires: tuple[tuple[str, frozenset[str], str], ...] = ()
    #: Extra requirements for one action (`(action, role, forbidden states, reason)`).
    action_requires: tuple[tuple[str, str, frozenset[str], str], ...] = ()
    #: Whether the effect can only be judged by the battery's power (no read-back from the device).
    local_readback: bool = False
    #: Per action that adds import, the roles that let the battery cover a peak again, written first (`executor`'s
    #: peak cover under a hold, and the first writes of every hand-back of that action).
    peak_first: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: `battery` (the plan's actions) or `export` (the export limit, `ACTION_CURTAIL_EXPORT`): an export control
    #: needs no battery reading or fuse headroom, since a lower export limit never adds import.
    kind: Literal["battery", "export"] = "battery"
    note: str = ""

    def role(self, name: str) -> Role:
        for role in self.roles:
            if role.name == name:
                return role
        raise KeyError(name)

    def supports(self, action: str) -> bool:
        return action == ACTION_SELF_CONSUME or action in self.states

    def wanted(self, action: str, rate_kw: float, watchdog_s: int = WATCHDOG_S) -> dict[str, Native]:
        """Every restored role's wanted value for the action (`SNAPSHOT` where the action leaves it)."""
        wanted: dict[str, Native] = {role.name: SNAPSHOT for role in self.roles if role.restored}
        if action != ACTION_SELF_CONSUME:
            for want in self.states[action](rate_kw, watchdog_s):
                wanted[want.role] = want.value
        return wanted


def native(role: Role, kw: float) -> float:
    """A power in the role's unit, rounded down to its step (never above what was asked)."""
    value = max(kw, 0.0) * role.scale
    if role.step:
        value = math.floor(value / role.step + 1e-9) * role.step
    return round(value, 6)


def reading(role: Role, state: str | None) -> str | None:
    """A role's state as compared: normalised, `None` when it is no reading."""
    if role.normalize is not None:
        return role.normalize(state)
    if state is None or state.strip().lower() in _NO_READING:
        return None
    return state


# --- Huawei's summary sensor ---------------------------------------------------------------------------------

_HUAWEI_RUN: Final = re.compile(r"^(Charging|Discharging) at (\d+)W")


def huawei_forcible(state: str | None) -> str | None:
    """"Stopped" -> `stop`, "Charging at 3000W for 60 minutes" -> `charge:3000`; `None` for anything else."""
    if state is None:
        return None
    if state == "Stopped":
        return "stop"
    found = _HUAWEI_RUN.match(state)
    if found is None:
        return None
    return f"{'charge' if found.group(1) == 'Charging' else 'discharge'}:{int(found.group(2))}"


# --- the brands --------------------------------------------------------------------------------------------------

_SIG_ENABLE = Role("remote_ems", "switch", _key("plant_remote_ems_enable", "switch"), restored=False)
_SIG_MODE = Role(
    "control_mode", "select", _key("plant_remote_ems_control_mode", "select"), restored=False,
    idle_when=frozenset({"unavailable"}),
)
_SIG_CHARGE = Role(
    "charge_limit", "number", _key("plant_ess_max_charging_limit", "number"), step=0.001,
    unset_as_rating="charge", unknown_is_unset=True,
)
_SIG_DISCHARGE = Role(
    "discharge_limit", "number", _key("plant_ess_max_discharging_limit", "number"), step=0.001,
    unset_as_rating="discharge", unknown_is_unset=True,
)
_SIG_MSC: Final = "Maximum Self Consumption"


def _sig(mode: str, **limits: Callable[[float], float]) -> Callable[[float, int], tuple[Want, ...]]:
    def build(rate_kw: float, _watchdog_s: int) -> tuple[Want, ...]:
        wants = [Want(name, fn(rate_kw)) for name, fn in limits.items()]
        return (*wants, Want("remote_ems", True), Want("control_mode", mode))

    return build


SIGENERGY_CONTROL: Final = BrandControl(
    brand="sigenergy",
    adapter=SIGENERGY,
    roles=(_SIG_ENABLE, _SIG_MODE, _SIG_CHARGE, _SIG_DISCHARGE),
    states={
        ACTION_HOLD: _sig(_SIG_MSC, discharge_limit=lambda _rate: 0.0),
        ACTION_GRID_CHARGE: _sig("Command Charging (Grid First)", charge_limit=lambda rate: native(_SIG_CHARGE, rate)),
        ACTION_DISCHARGE_HOUSE: _sig(_SIG_MSC, charge_limit=lambda _rate: 0.0),
        ACTION_EXPORT_DISCHARGE: _sig(
            "Command Discharging (PV First)", discharge_limit=lambda rate: native(_SIG_DISCHARGE, rate)
        ),
    },
    enter_order=("charge_limit", "discharge_limit", "remote_ems", "control_mode"),
    restore_order=("control_mode", "remote_ems", "charge_limit", "discharge_limit"),
    own_states={"EMS work mode": frozenset({"remote_ems"})},
    requires=(("remote_ems", frozenset({"off"}), "remote_ems_already_on"),),
    # A hold is the discharge limit at 0: lifting it lets the battery cover a peak at once (SigenStor covers each
    # phase on its own). A grid charge stops with the mode back at Maximum Self Consumption.
    peak_first={ACTION_HOLD: ("discharge_limit",), ACTION_GRID_CHARGE: ("control_mode",)},
    note="Remote EMS replaces the app's mode only while it is on; SpotNav takes a plant in Maximum Self"
    " Consumption only, so turning it off hands back the mode it found.",
)

# The normal state has no wanted value for these: the switch goes off, the select to MSC (only while it can).
_SIGENERGY_NORMAL: Final = {"remote_ems": False, "control_mode": _SIG_MSC}

_SE_MODE = Role("command_mode", "select", _key("storage_command_mode", "select"))
_SE_CHARGE = Role("charge_limit", "number", _key("storage_charge_limit", "number"), scale=1000.0, step=1.0)
_SE_DISCHARGE = Role("discharge_limit", "number", _key("storage_discharge_limit", "number"), scale=1000.0, step=1.0)
_SE_TIMEOUT = Role("command_timeout", "number", _key("storage_command_timeout", "number"), step=1.0)
_SE_CONTROL_MODE = Role("control_mode", "select", _key("storage_control_mode", "select"), restored=False)
_SE_POLICY = Role("ac_charge_policy", "select", _key("ac_charge_policy", "select"), restored=False)


def _se(mode: str, **limits: Callable[[float], float]) -> Callable[[float, int], tuple[Want, ...]]:
    def build(rate_kw: float, watchdog_s: int) -> tuple[Want, ...]:
        wants = [Want(name, fn(rate_kw)) for name, fn in limits.items()]
        return (Want("command_timeout", float(watchdog_s)), *wants, Want("command_mode", mode))

    return build


SOLAREDGE_CONTROL: Final = BrandControl(
    brand="solaredge",
    adapter=SOLAREDGE,
    roles=(_SE_TIMEOUT, _SE_CHARGE, _SE_DISCHARGE, _SE_MODE, _SE_CONTROL_MODE, _SE_POLICY),
    states={
        ACTION_HOLD: _se("Charge from Solar Power"),
        ACTION_GRID_CHARGE: _se("Charge from Solar Power and Grid", charge_limit=lambda rate: native(_SE_CHARGE, rate)),
        ACTION_DISCHARGE_HOUSE: _se("Discharge to Minimize Import"),
        ACTION_EXPORT_DISCHARGE: _se(
            "Discharge to Maximize Export", discharge_limit=lambda rate: native(_SE_DISCHARGE, rate)
        ),
    },
    enter_order=("command_timeout", "charge_limit", "discharge_limit", "command_mode"),
    restore_order=("command_mode", "charge_limit", "discharge_limit", "command_timeout"),
    watchdog_roles=("command_timeout", "command_mode"),
    requires=(("control_mode", frozenset({"Remote Control"}), "not_in_remote_control"),),
    peak_first={ACTION_HOLD: ("command_mode",), ACTION_GRID_CHARGE: ("command_mode",)},
    action_requires=((ACTION_GRID_CHARGE, "ac_charge_policy", frozenset({"Disabled"}), "ac_charge_disabled"),),
    note="Remote Control only, as the owner set it; the command timeout returns the inverter to its default mode.",
)

_HW_FORCIBLE = Role(
    "forcible", "service", _key("forcible_charge_discharge_write"), restored=False,
    services={"charge": "huawei_solar.forcible_charge", "discharge": "huawei_solar.forcible_discharge",
              "stop": "huawei_solar.stop_forcible_charge"},
    normalize=huawei_forcible,
)
_HW_DISCHARGE = Role(
    "discharge_power", "number", _key("storage_maximum_discharging_power", "number"), scale=1000.0, step=1.0
)
_HW_CHARGE = Role("charge_power", "number", _key("storage_maximum_charging_power", "number"), scale=1000.0, step=1.0)


def _hw_run(kind: str) -> Callable[[float, int], tuple[Want, ...]]:
    def build(rate_kw: float, _watchdog_s: int) -> tuple[Want, ...]:
        return (Want("forcible", f"{kind}:{int(native(_HW_DISCHARGE, rate_kw))}"),)

    return build


HUAWEI_CONTROL: Final = BrandControl(
    brand="huawei",
    adapter=HUAWEI,
    roles=(_HW_FORCIBLE, _HW_DISCHARGE, _HW_CHARGE),
    states={
        ACTION_HOLD: lambda _rate, _w: (Want("forcible", "stop"), Want("discharge_power", 0.0)),
        ACTION_GRID_CHARGE: _hw_run("charge"),
        ACTION_DISCHARGE_HOUSE: lambda _rate, _w: (Want("forcible", "stop"), Want("charge_power", 0.0)),
        ACTION_EXPORT_DISCHARGE: _hw_run("discharge"),
    },
    enter_order=("discharge_power", "charge_power", "forcible"),
    restore_order=("forcible", "charge_power", "discharge_power"),
    watchdog_roles=("forcible",),
    requires=(("forcible", frozenset({"stop"}), "forcible_charge_running"),),
    peak_first={ACTION_HOLD: ("discharge_power",), ACTION_GRID_CHARGE: ("forcible",)},
    note="Services and numbers need the installer password; a forcible charge ends by itself after its duration.",
)

_SX_CONTROL = Role(
    "power_control", "select", _key("remotecontrol_power_control", "select"),
)
_SX_POWER = Role("active_power", "number", _key("remotecontrol_active_power", "number"), scale=1000.0, step=100.0)
_SX_REPEAT = Role("autorepeat", "number", _key("remotecontrol_autorepeat_duration", "number"), step=1.0)
_SX_TRIGGER = Role("trigger", "button", _key("remotecontrol_trigger", "button"), restored=False)


def _sx(option: str, sign: float = 0.0) -> Callable[[float, int], tuple[Want, ...]]:
    def build(rate_kw: float, watchdog_s: int) -> tuple[Want, ...]:
        wants = [Want("autorepeat", float(watchdog_s)), Want("power_control", option)]
        if sign:
            wants.append(Want("active_power", sign * native(_SX_POWER, rate_kw)))
        return (*wants, Want("trigger", True))

    return build


SOLAX_CONTROL: Final = BrandControl(
    brand="solax",
    adapter=SOLAX,
    roles=(_SX_REPEAT, _SX_POWER, _SX_CONTROL, _SX_TRIGGER),
    states={
        ACTION_HOLD: _sx("Enabled No Discharge"),
        ACTION_GRID_CHARGE: _sx("Enabled Battery Control", 1.0),
        ACTION_DISCHARGE_HOUSE: _sx("Enabled Self Use"),
        ACTION_EXPORT_DISCHARGE: _sx("Enabled Battery Control", -1.0),
    },
    enter_order=("autorepeat", "active_power", "power_control", "trigger"),
    restore_order=("power_control", "trigger", "active_power", "autorepeat"),
    own_states={
        "remote control": frozenset(
            {"Enabled No Discharge", "Enabled Battery Control", "Enabled Self Use", "Enabled Power Control"}
        )
    },
    watchdog_roles=("trigger",),
    requires=(("power_control", frozenset({"Disabled"}), "remote_control_in_use"),),
    peak_first={ACTION_HOLD: ("power_control", "trigger"), ACTION_GRID_CHARGE: ("power_control", "trigger")},
    local_readback=True,
    note="The integration holds the values and sends them on the trigger; only the battery's power shows the"
    " effect.",
)

_SIG_EXPORT = Role(
    "export_limit", "number", _key("plant_grid_point_maximum_export_limitation", "number"), step=0.001,
    unset_as_rating="export", unknown_is_unset=True,
)

SIGENERGY_EXPORT_CONTROL: Final = BrandControl(
    brand="sigenergy",
    adapter=SIGENERGY,
    roles=(_SIG_EXPORT,),
    states={ACTION_CURTAIL_EXPORT: lambda _rate, _w: (Want("export_limit", 0.0),)},
    enter_order=("export_limit",),
    restore_order=("export_limit",),
    # The battery control's own Remote EMS is not "managed by its own system" for the export limit either.
    own_states={"EMS work mode": frozenset({"remote_ems"})},
    kind="export",
    note="Grid Export Limitation (40038) applies in every EMS mode and needs the plant's grid sensor; a never-set"
    " limit is restored as the plant's maximum output.",
)

#: The restore value of a role that has no snapshot of its own (never read before: a select unavailable while its
#: switch is off, a service, a button).
NORMAL: Final[Mapping[str, Mapping[str, Native]]] = {
    "sigenergy": _SIGENERGY_NORMAL,
    "huawei": {"forcible": "stop"},
    "solax": {"trigger": True},
}

CONTROLS: Final[tuple[BrandControl, ...]] = (SIGENERGY_CONTROL, SOLAREDGE_CONTROL, HUAWEI_CONTROL, SOLAX_CONTROL)


#: The export-limit controls: only where the brand's adapter already knows the entity (`EXPORT_LIMIT_TABLE` lists
#: the brands seen with a local export limit, mapped or not).
EXPORT_CONTROLS: Final[tuple[BrandControl, ...]] = (SIGENERGY_EXPORT_CONTROL,)


def control_for(brand: str | None) -> BrandControl | None:
    """The brand's control, `None` for a brand SpotNav writes nothing to."""
    return next((control for control in CONTROLS if control.brand == brand), None)


def export_control_for(brand: str | None) -> BrandControl | None:
    """The brand's export-limit control, `None` for a brand SpotNav has none for."""
    return next((control for control in EXPORT_CONTROLS if control.brand == brand), None)


#: Brands with a local export limit in their Home Assistant integration, from each source (2026-11-15, shallow
#: clones). `mapped`: SpotNav has the control (`EXPORT_CONTROLS`); the rest are noted for later, not written.
EXPORT_LIMIT_TABLE: Final[tuple[Mapping[str, Any], ...]] = (
    {"brand": "sigenergy", "integration": "sigen (TypQxQ 2.0.3, 41182ec)", "mapped": True,
     "entity": "number `plant_grid_point_maximum_export_limitation` (Grid Export Limitation, kW, register 40038)",
     "note": "Every EMS mode, no Remote EMS needed; needs the plant's grid sensor; disabled by default."},
    {"brand": "solaredge", "integration": "solaredge_modbus_multi (WillCodeForCats fa590ea)", "mapped": False,
     "entity": "number `<model>_<serial>_site_limit` (Site Limit, W) with select `<model>_<serial>_limit_control_mode`",
     "note": "Only with the integration's advanced 'site limit control' option; the limit is available only while"
     " an export-control mode is set."},
    {"brand": "huawei", "integration": "huawei_solar (wlcrs 2412b4d)", "mapped": False,
     "entity": "services `huawei_solar.set_zero_power_grid_connection` / `set_maximum_feed_grid_power` (W)",
     "note": "Active power control of the inverter or EMMA; installer password; no plain number to read back."},
    {"brand": "solax", "integration": "solax_modbus (wills106 4acd644)", "mapped": False,
     "entity": "number `<hub>_export_control_user_limit` (Export Control User Limit, W, register 0x42)",
     "note": "A setting register, not the remote control's local values; its range differs per generation."},
    {"brand": "sungrow", "integration": "mkaiser Sungrow-SHx YAML package (13a7622), not an integration", "mapped": False,
     "entity": "template number `uid_export_power_limit` (W, register 13073) with modbus switch"
     " `sg_export_power_limit_switch` (13086)",
     "note": "The limit applies only while the switch is on."},
    {"brand": "victron", "integration": "victron_gx (victron-mqtt)", "mapped": False,
     "entity": "number `system_ess_max_feed_in_power` (/Settings/CGwacs/MaxFeedInPower, W, -1 no limit)",
     "note": "A setting stored on the GX; Multi RS also has switch `multi_disable_feed_in`. The community Modbus"
     " integration (sfstar/hass-victron 32fb345) has `settings_ess_maxfeedinpower` (register 2706, W, -1 no limit)."},
    {"brand": "goodwe", "integration": "core goodwe (library goodwe 66dfcdd)", "mapped": False,
     "entity": "number `grid_export_limit` (W on ET/EH, register 47510; % on DT, register 40328)",
     "note": "A setting written to the inverter; the unit depends on the model."},
    {"brand": "fronius", "integration": "core fronius (Solar API)", "mapped": False, "entity": None,
     "note": "The core integration reads only; an export limit needs Modbus, not checked."},
)


def table() -> list[dict[str, Any]]:
    """The command table, per brand and action, for diagnostics and the report."""
    rows: list[dict[str, Any]] = []
    for control in CONTROLS:
        for action in CONTROL_ACTIONS:
            wants = control.states[action](5.0, WATCHDOG_S)
            rows.append({"brand": control.brand, "action": action, "writes": {w.role: w.value for w in wants}})
        rows.append(
            {"brand": control.brand, "action": ACTION_SELF_CONSUME,
             "writes": dict(NORMAL.get(control.brand, {})) | {"*": SNAPSHOT}}
        )
    return rows


def export_table() -> list[dict[str, Any]]:
    """The export-limit command table and the brands seen with a local export limit, for diagnostics."""
    rows: list[dict[str, Any]] = []
    for control in EXPORT_CONTROLS:
        wants = control.states[ACTION_CURTAIL_EXPORT](0.0, WATCHDOG_S)
        rows.append({"brand": control.brand, "action": ACTION_CURTAIL_EXPORT, "writes": {w.role: w.value for w in wants}})
        rows.append({"brand": control.brand, "action": ACTION_SELF_CONSUME, "writes": {"*": SNAPSHOT}})
    return rows


def service_call(
    method: RoleMethod, entity_id: str | None, value: Native, *, device_id: str | None = None,
    services: Mapping[str, str] | None = None, watchdog_s: int = WATCHDOG_S,
) -> tuple[str, str, dict[str, Any]]:
    """The Home Assistant service call `(domain, service, data)` that writes `value` to a role.

    Numbers, selects, switches and buttons use HA's own entity services (core `number.set_value`,
    `select.select_option`, `switch.turn_on`/`turn_off`, `button.press`). A service role (Huawei's forcible charge)
    turns `charge:<W>` / `discharge:<W>` / `stop` into its integration's service, for the battery's device, with the
    watchdog as the duration in minutes."""
    if method == "number":
        return "number", "set_value", {"entity_id": entity_id, "value": value}
    if method == "select":
        return "select", "select_option", {"entity_id": entity_id, "option": value}
    if method == "switch":
        return "switch", "turn_on" if value is True else "turn_off", {"entity_id": entity_id}
    if method == "button":
        return "button", "press", {"entity_id": entity_id}
    if not isinstance(value, str) or services is None or device_id is None:
        raise ValueError(f"no service call for {value!r}")
    kind, _, power = value.partition(":")
    full = services.get(kind)
    if full is None:
        raise ValueError(f"no service for {kind!r}")
    domain, service = full.split(".", 1)
    data: dict[str, Any] = {"device_id": device_id}
    if kind != "stop":
        data |= {"power": int(power), "duration": max(1, min(1440, watchdog_s // 60))}
    return domain, service, data
