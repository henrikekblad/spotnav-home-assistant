"""What SpotNav writes to a home battery for each of the plan's actions, per brand (behind the site's gate,
`gate.py`).

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
  back. The select's options are TypQxQ's (`select.py` lines 105-114 at both pinned commits, mapped to Appendix 6
  at lines 116-137): "Maximum Self Consumption" (2), "Command Charging (Grid First)" (3), "Command Charging (PV
  First)" (4), "Command Discharging (PV First)" (5), "Command Discharging (ESS First)" (6).
  **Which limit acts in which mode depends on the protocol generation.** Sigenergy Modbus Protocol V2.7, plant
  parameters 23-25 (`sig27.txt` lines 1133-1149): the ESS max charging limit (40032) "takes effect when Remote EMS
  control mode (40031) is 3 or 4", the max discharging limit (40034) "is 5 or 6", the PV max power limit (40036,
  never written here) "3, 4, 5 or 6". V2.9 (2026-05-13), Table 5-2-1 rows 1-3 (`sig29.txt` lines 1120-1138): all
  three "take effect globally regardless of the EMS operating mode". The grid import and export limits (40038/40040)
  act in every mode in both (`SIGENERGY_LIMIT_MODES`). A grid charge (mode 3, charge limit) and an export discharge
  (mode 5, discharge limit) keep to both generations. A hold has two variants (`SIGENERGY_HOLDS`), tried in order
  by the verification: `msc_no_discharge`, Maximum Self Consumption with the discharge limit at 0 (valid on
  V2.9-era firmware and seen obeyed on SPC117 with Remote EMS on, TypQxQ discussion #424; the sun is still stored;
  its peak cover lifts the limit), then `pv_first`, mode 4 with the charge limit at 0. Appendix 6 names mode 4
  "Command charging (consume PV power first)" (`sig27.txt` lines 2180-2183, `sig29.txt` line 3346): a charge at the
  charge limit, from the sun first and then the grid (haeo's Sigenergy walkthrough and Roving-Ronin's
  EMS-Events-Card use it so). At a limit of 0 it charges nothing, and the discharge limit does not act in it under
  V2.7 (it acts only in 5 and 6) while the plant discharges in no Command Charging mode: the battery stands idle and
  a sun surplus is exported. That is within V2.7's scope and V2.9's, and it is also what a command left standing
  after Home Assistant died does. Its peak cover writes the mode back to Maximum Self Consumption, where the plant
  balances each phase itself. A `pv_first` hold whose battery charges anyway is handed back
  (`executor.HOLD_CHARGE_S`) and fails its verification. A discharge to the house has no mode in V2.7 ("cover the
  house, store nothing"): it stays Maximum Self Consumption with the charge limit at 0, valid under V2.9 and at
  worst ignored under V2.7 (the battery then also stores a sun surplus, as in self-consumption). V2.8 removed 41500
  (Remote EMS dispatch enable, never used here); V2.9 added
  0x08 V2G to 40031. 40049, Active power regulation gradient (%/s of the rated power, V2.8+), bounds how fast the
  plant changes its output and so its reaction to a mode or limit change; SpotNav never writes it. A plant is taken in Maximum Self Consumption (`adapters.SIGENERGY`'s mode rule), or in AI mode once the
  person acknowledged the takeover (`BrandControl.takeover`); turning Remote EMS off lands in Maximum Self
  Consumption either way (research §2.2), so the hand-back is clean and a plant taken from AI mode is not put back
  in it (the person is reminded to set it again in the app). The discharge cut-off SoC is never
  written: with it above the SoC the plant grid-charges to reach it (research §3.1 D).
  **Per phase** (`SIGENERGY_PHASES`, seen on the owner's plant 2026-11-18, SPC117, sigen 2.0.3): with independent
  phase power control on (40030, switch `plant_independent_phase_power_control_enable`, disabled by default; it read
  on as found) Remote EMS in the control mode "PCS Remote Control" (40031 = 0) follows the plant's active power
  target per phase (40008/40010/40012, numbers `plant_phase_{a,b,c}_active_power_fixed_target`, kW, disabled by
  default; available also while Remote EMS is off). The target is the plant's AC power on that phase: negative
  takes from the grid on it (B at -1 kW: L2 imported 1.0 kW more, the battery charged 0.7 kW), positive puts out on it
  (B at +1 kW: L2 exported 0.81 kW with 0.18 kW of house on it), all at 0 leaves the house on the grid. Phases A, B, C
  are the meter's L1, L2, L3 there, and a new target settles in under 5 s. The plant's own power per phase
  (`adapters.SIGENERGY.plant_phase_power`) reads the target back as power. Whether the grid import limit (40040)
  acts in this mode is not known, so it is never counted on here. The plant's **total** active power target (40001,
  number `plant_active_power_fixed_target`) acts in the same mode, so the phased control owns it too and keeps it at
  0, and the plain control refuses to start while it or a phase target reads anything else (`watched`): Remote EMS
  on passes through PCS Remote Control for some seconds when 40031 still holds 0, and a target left there by
  someone would run. **How often they may be written:** Sigenergy Modbus Protocol V2.9 lists 40001 and
  40008-40012 in Table 5-2-2, "Plant parameter setting power dispatch register definition (holding register)"
  (`sig29.txt` lines 1244-1300): "For these registers to take effect ... remote EMS enable (40029) is set to 0x01
  ... remote EMS control mode (40031) is set to 0x00", and "These registers only control the power of PCS and is
  independent of loads and grid sensors". It says nothing of how often they may be written, nor whether they are
  kept over a power cycle; the only rate it gives is the bus's own, "Minimum Request period : 1000 ms" (lines
  570-582). They are dispatch registers, meant for an EMS that steers the plant, but with no word on wear their
  daily cap is a moderate `SETPOINT_WRITES_UNSTATED` (500), not the 2000 of a setpoint documented as volatile.
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
* **Victron ESS** (`VICTRON_CONTROL`, a GX with a VE.Bus Multi or Quattro and the ESS assistant; **not tested on a
  plant**), `victron_gx` in Home Assistant core `c87112a6731db4df611917e171787d43b802b00f` (2026-11-08, library
  victron-mqtt 2026.11.1, tomer-w/victron_mqtt `2e25f545c5d58ba105f6c7bb0ef0da481930d51d`, `_victron_topics.py`), and
  the HACS `victron_mqtt` (tomer-w/ha-victron-mqtt `82cce7997c5b6f159b5a988fa622b0a509412cd2`, 2026.11.4, the same
  short ids; its operation mode `read_only` makes every control a sensor, which no role here matches, while core
  always runs `full`). Every entity is `<installation>_<device>_<short id>` and is enabled by default; a write is an MQTT
  publish to the GX's `W/` topic (`writable_metric.py` `set`, always sent, also for the value it already has), and
  the state changes only when the GX publishes the value back. Victron's own page "ESS mode 2 and 3"
  (https://www.victronenergy.com/live/ess:ess_mode_2_and_3, read 2026-11-18, the raw text's sha256 `a8a3bfd8...`)
  is the source of what each one does:
  * **Mode 2** (the GX runs its own loop; SpotNav moves its control points, all settings stored on the GX, so
    `setting` writes): number `system_ac_power_set_point` (`/Settings/CGwacs/AcPowerSetPoint`, W, step 10, the
    **grid's** total: "Positive: take power from grid. Negative: send power to grid", default 50), number
    `system_ess_max_charge_power` (`/Settings/CGwacs/MaxChargePower`, W, step 50, -1 no limit: the most the Multi
    charges from AC), number `system_ess_max_inverter_power_limit` (`/Settings/CGwacs/MaxDischargePower`, W, step
    50, "-1: No limit. Any positive number: Maximum power in Watt that the Multi will feed to the loads"). A hold is
    the discharge power at 0 (the sun is still stored); a discharge to the house is the charge power at 0 (the GX
    covers the house as it always does; a sun surplus on the AC side is exported, one from an MPPT on the DC side
    is still stored: the limit is the Multi's); a grid charge is the charge power at the rate **and** the grid
    setpoint at the house's net load now plus the rate (`BrandControl.house_states`: the setpoint is the grid's
    power, not the battery's, so the house is added; the charge power is what bounds the battery's draw when the
    house falls or Home Assistant stops, the setpoint what bounds the site's import while it runs; when the house
    rises the battery takes less, and discharges once the house alone passes the setpoint); a discharge to the grid
    is the grid setpoint at the house's net load less the rate. Nothing of this falls back by itself: the settings
    stay until written, so the grid charge keeps the HA-down cap of a plant that puts it all on one phase
    (`grid_charge_phases` 1: the entities do not say how many phases the Multis are on). The page warns that
    2700 (this setpoint) is logged to VRM and wears the GX's flash when written repeatedly (Venus 3.50 added a
    volatile 2716, `/Overrides/Setpoint` on the hub4 service, whose life the page does not describe: not used), so
    the setpoint is written only when the rate changes, never to follow the house.
  * **Mode 3** (`VICTRON_PHASES`, the charge per phase): select `system_ess_mode` (`/Settings/CGwacs/Hub4Mode`:
    `phase_compensation_enabled` 1, `phase_compensation_disabled` 2, `external_control` 3), numbers
    `vebus_ac_power_setpoint_l1..l3` (`/Hub4/L1..L3/AcPowerSetpoint` on the VE.Bus service, Modbus 37/40/41, W,
    -10000 to 10000), switches `vebus_hub4_disable_charge` and `vebus_hub4_disable_feed_in` (38/39). In external
    control the GX's loop stops and each Multi holds the power **on its AC input** at its setpoint: "the point of
    control is the AC-input, not the inverter/charger itself. Power to/from AC-input = Power to/from battery + Power
    to/from AC-output", "Positive: Take power from the grid. Negative: Send power to the grid." So a setpoint is not
    a battery target: a charge of `c` on a phase is the load on that Multi's AC output plus `c`
    (`PhaseControl.target_kind` `grid`, `behind`: sensors `vebus_inverter_output_power_l1..l3`, `/Ac/Out/Lx/P`; the
    power at the point is `point`: `vebus_inverter_input_power_l1..l3`, `/Ac/ActiveIn/Lx/P`, + from the grid by
    Victron's convention, which the verification's probe checks). Loads wired beside the Multi are not behind the
    point and are not counted: they are the site meter's, and the live fuse clamp sees them. Outside external
    control the GX writes the setpoints and both flags itself, many times a minute, so SpotNav writes them only in
    external control, never parks them at 0 and never hands them back (`PhaseControl.park` false): the hand-back is
    one write, the ESS mode back to the value found, and the GX takes all three phases over at once.
    **The watchdog:** "These registers must be written once every 60 seconds, or the Multi will go into Passthru"
    (3.2 a; 6.2 and 6.3 repeat it for D-Bus: "the AcPowerSetpoint value has to be written at least once every 60
    seconds"). Passthru is the Multi neither charging nor discharging: its AC output's loads are fed from the grid.
    So the three setpoints are renewed every `VICTRON_RENEW_S` (each is then written again within about 30 s), and
    a charge per phase stops by itself within 60 s of the last write (`PhaseControl.silence_fallback_s`): this is
    the one recipe whose HA-down cap is lifted to the fuse itself (`gate.grid_charge_phase_caps_kw`), the live
    clamp per phase kept. After such a stop the plant stays in passthru (the ESS mode is a setting) until Home
    Assistant is back and hands the mode back: it then neither stores the sun nor covers the house. Both flags are
    written off for the charge (with feed-in allowed the battery covers a load that passes the setpoint, instead of
    the grid). The setpoints are not stored in flash (the GX's own loop writes them all the time in its modes) and
    are made for a control loop: `SETPOINT_WRITES_WATCHED` a day.
  * The minimum SoC (`system_ess_min_soc_limit`) is read as the floor (`adapters.VICTRON.reserves`) and never
    written. A plant is taken only in an optimized ESS mode with Dynamic ESS off (`adapters.VICTRON`'s mode rules);
    one found in external control is someone else's.
  * **Not mapped: the community Modbus integration** (`victron`, sfstar/hass-victron
    `32fb345f383f4d20efbffb5209c4c1868f5593b0`). It has the same registers (`const.py`: `settings_ess_acpowersetpoint`
    2700, `settings_ess_maxdischargepower` 2704, `settings_ess_mode` 2902, `vebus_L1..L3_acpowersetpoint` 37/40/41,
    `vebus_disablecharge` 38, `vebus_disablefeedin` 39), but only with its advanced options, and three things stand
    in the way: 2704 is read as an unsigned register, so "no limit" (-1) reads as a large number that cannot be
    written back; a negative number is written as 65535 plus the value (`number.py` `async_set_native_value`), one
    off from two's complement; and its ESS mode select names Hub4Mode's values 1 and 2 `SELF_CONSUMPTION` and
    `KEEP_CHARGED`. It has no entity for the maximum charge power.
* GoodWe, Enphase and brands without an adapter: **none**. (GoodWe's and Enphase's controls in core are whole-mode
  switches or a reserve trick, see `battery_cycle_cost_and_modes_2026-11-13.md` part 3.)

Pure: no Home Assistant import. Nothing here writes; it says what would be written.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

from .adapters import HUAWEI, SIGENERGY, SOLAREDGE, SOLAX, VICTRON, Adapter, Match, _key
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

RoleMethod = Literal["number", "select", "switch", "button", "service", "sensor"]
#: What a role is, for how often it may be written in a day (`BrandControl.write_caps`): a `setting` (a limit, a mode,
#: a switch: possibly stored in flash) or a `setpoint` made for continuous outside control.
WriteClass = Literal["setting", "setpoint"]
WRITE_CLASSES: Final[tuple[WriteClass, ...]] = ("setting", "setpoint")
#: Writes a day per class, outside restores and watchdog renewals. A setting, and anything of a brand or role not
#: checked: 96. A setpoint whose maker says it is volatile or made for continuous control: about 2000 (still a
#: runaway guard: reaching it hands back). A setpoint the maker's protocol says nothing about: 500.
SETTING_WRITES: Final = 96
SETPOINT_WRITES_CONTINUOUS: Final = 2000
SETPOINT_WRITES_UNSTATED: Final = 500
#: A setpoint the maker says must be written again within a minute (Victron's Mode 3: more than 1440 a day by design;
#: the renewals themselves are not counted, this bounds the changes).
SETPOINT_WRITES_WATCHED: Final = 3000
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
    #: Which daily write budget its writes count against (`BrandControl.write_cap`).
    write_class: WriteClass = "setting"
    #: How near a read must be to a value to count as it (in the entity's unit), where the entity's own step is too
    #: coarse for that: a value one unit off means something else (Victron's -1, no limit, beside 0). `None`: the
    #: entity's step, else `step`.
    tolerance: float | None = None


@dataclass(frozen=True, slots=True)
class Want:
    """The state a role must be in for an action: a native value, or `SNAPSHOT`."""

    role: str
    value: Native


@dataclass(frozen=True, slots=True)
class HoldVariant:
    """One way a brand can hold (no discharge): its writes and the roles that let the battery cover a peak again.
    The verification tries a brand's variants in order and control uses the first that passed."""

    name: str
    build: Callable[[float, int], tuple[Want, ...]]
    peak_first: tuple[str, ...]
    note: str = ""
    #: The battery stands idle (it stores no sun either): a battery that charges under it did not take the hold
    #: (`monitor.judge`, `executor.HOLD_CHARGE_S`). `False`: the sun is still stored.
    idle: bool = False


#: A power per phase in the order of the site's meter: L1, L2, L3 (kW).
Phases = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class PhaseControl:
    """A plant that takes a power target per phase: what a grid charge sized to each phase's own headroom needs
    (`gate.grid_charge_phase_caps_kw`). Data only; the executor, the monitor and the verification know no brand.

    A brand fills in the three target roles **in the order of the site's meter** (the role for L1 first: a plant
    whose phase A is not the meter's L1 lists them in the meter's order), the sign and unit of a target, the state
    that makes the plant follow the targets, and what must read so for them to act. The control that runs it is
    `BrandControl.with_phases(True)`: its grid charge writes a target per phase, every other action writes the
    targets to 0, and every hand-back stops the targets first. The plant's power per phase, by which the effect is
    judged, is the adapter's `plant_phase_power` (positive for output). It is used only once the verification's
    mode `per_phase` passed on this firmware (`verify.py`: a small target on each phase in turn, read back as that
    phase's power with the right sign, on the meter's phase of that name)."""

    #: The targets' roles for L1, L2, L3 (numbers, never restored: handed back as 0).
    targets: tuple[Role, Role, Role]
    #: What a target reads for a charge of 1 kW from that phase, in kW before the role's `scale` (-1: negative
    #: takes from the grid, positive puts out).
    charge_sign: float
    #: The state that makes the plant follow the targets, beside the targets themselves: wants, or a function of the
    #: charge per phase for a brand whose state depends on it (a mode per direction, a total beside the phases).
    follow: tuple[Want, ...] | Callable[[Phases], tuple[Want, ...]]
    #: What a target sets: the `plant`'s own power on that phase (judged on the adapter's `plant_phase_power`), or
    #: the `grid`'s power on it (a grid setpoint per phase; judged on the site meter's phase, the house included).
    target_kind: Literal["plant", "grid"] = "plant"
    #: Other targets that act in the same state and must be 0 (a total beside the phases): owned, written to 0
    #: before anything else in every action, handed back as 0.
    zero: tuple[Role, ...] = ()
    #: Roles renewed to keep a brand's watchdog alive while the phased control runs (added to the control's own).
    watchdog_roles: tuple[str, ...] = ()
    #: Mode-rule labels and the states the phased control's own commands leave them in (added to the control's own).
    own_states: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: Roles that must read one of these states for the targets to act, never written (`(role, states, reason)`).
    requires: tuple[tuple[Role, frozenset[str], str], ...] = ()
    #: The orders with the targets in them (`BrandControl.enter_order`, `restore_order`), and the roles that stop a
    #: charge per phase, written first on every hand-back and change of action (`BrandControl.peak_first`).
    enter_order: tuple[str, ...] = ()
    restore_order: tuple[str, ...] = ()
    stop_first: tuple[str, ...] = ()
    #: Further roles only the phased control writes (what `follow` sets beside the brand's own roles).
    roles: tuple[Role, ...] = ()
    #: Whether the targets are written to 0 in every other action and handed back as 0. `False` where the plant
    #: itself writes them outside the state that follows them: they are then SpotNav's only during a charge per
    #: phase, and are left to the plant on every change of action and hand-back.
    park: bool = True
    #: For a `grid` target: sensors of the load behind the target's point on L1, L2, L3 (what the point carries
    #: beside the plant's own charge; never written), and of the power at the point (+ from the grid). A target is
    #: then that load plus the charge, the plant's own power on a phase the load less the point's power, and the
    #: effect is judged at the point against the target written (`grid_phases`). `scale` as for a written role.
    behind: tuple[Role, Role, Role] | None = None
    point: tuple[Role, Role, Role] | None = None
    #: How often the `watchdog_roles` are renewed while the phased control runs a charge per phase (s); `None`:
    #: `WATCHDOG_RENEW_S`.
    watchdog_renew_s: int | None = None
    #: The plant stops following the targets by itself this long after the last write of them (s): the maker's own
    #: dead-man. `None`: the targets stay until written. Read by the gate (`gate.grid_charge_phase_caps_kw`): only
    #: with it may a phase's HA-down cap be the fuse itself.
    silence_fallback_s: int | None = None
    #: The three targets exist only on a three-phase plant (an entity per phase the plant has): finding them is
    #: what shows the plant's phases, where the brand has no other reading of them.
    three_phase_by_targets: bool = False
    #: The largest size a `grid` target is written at (kW): the entity's own range, which a write beyond would be
    #: refused by. A target cut to it leaves the battery less than its charge, never more.
    limit_kw: float | None = None

    def names(self) -> tuple[str, str, str]:
        return (self.targets[0].name, self.targets[1].name, self.targets[2].name)

    def value(self, index: int, charge_kw: float, behind_kw: float = 0.0) -> float:
        """The native target for a charge of `charge_kw` on that phase (negative: a discharge), its size rounded
        down to the role's step. For a `grid` target the power at the point: `behind_kw`, the load behind it, plus
        the charge."""
        if self.target_kind == "grid":
            power = behind_kw + charge_kw
            if self.limit_kw is not None:
                power = max(min(power, self.limit_kw), -self.limit_kw)
            size = native(self.targets[index], abs(power))
            return 0.0 if size == 0 else round((-self.charge_sign if power < 0 else self.charge_sign) * size, 6)
        size = native(self.targets[index], abs(charge_kw))
        sign = self.charge_sign if charge_kw >= 0 else -self.charge_sign
        return 0.0 if size == 0 else round(sign * size, 6)

    def wants(self, charge_kw: Phases, behind_kw: Phases | None = None) -> tuple[Want, ...]:
        """A charge per phase: the state that follows the targets, the other targets at 0, and each phase's target
        (for a `grid` target with the load behind each point, `behind_kw`, 0 where it is not given)."""
        follow = self.follow(charge_kw) if callable(self.follow) else self.follow
        behind = behind_kw or (0.0, 0.0, 0.0)
        return (
            *follow, *(Want(role.name, 0.0) for role in self.zero),
            *(Want(role.name, self.value(index, charge_kw[index], behind[index]))
              for index, role in enumerate(self.targets)),
        )

    def idle(self) -> tuple[Want, ...]:
        """Every target at 0: what every action other than a charge per phase writes (nothing where the targets are
        not parked)."""
        return tuple(Want(role.name, 0.0) for role in (*self.zero, *self.targets)) if self.park else ()

    def owned(self) -> tuple[Role, ...]:
        """Every role the phased control adds: the targets, the others kept at 0, what `follow` writes, what must
        read so, and for a `grid` target the sensors at and behind its point."""
        return (
            *self.zero, *self.roles, *self.targets, *(role for role, _states, _why in self.requires),
            *(self.behind or ()), *(self.point or ()),
        )

    def grid_phases(self, states: Mapping[str, str | None]) -> tuple[Phases, Phases] | None:
        """For a `grid` target with its sensors: the load behind the point and the power at the point on L1, L2, L3
        (kW, the point's + from the grid), from the roles' states by name; `None` when the recipe has no such
        sensors or one of them reads nothing."""
        if self.behind is None or self.point is None:
            return None
        out: list[Phases] = []
        for roles in (self.behind, self.point):
            values: list[float] = []
            for role in roles:
                text = reading(role, states.get(role.name))
                try:
                    value = float(text) / role.scale if text is not None else math.nan
                except ValueError:
                    value = math.nan
                if not math.isfinite(value):
                    return None
                values.append(value)
            out.append((values[0], values[1], values[2]))
        return out[0], out[1]


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
    #: Mode-rule labels and the states of the plant's own optimiser that SpotNav may take the plant over from, only
    #: once the person acknowledged it (`ExecInputs.takeover`, the site's `battery_control_ai_takeover_ack`):
    #: Sigenergy's AI mode. Its hand-back lands in the brand's plain mode, not back in the optimiser.
    takeover: Mapping[str, frozenset[str]] = field(default_factory=dict)
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
    #: How the plant spreads a grid charge over the phases: as many phases, evenly (the HA-down grid charge cap,
    #: `gate.grid_charge_cap_kw`); 1 (all on one phase) where the brand does not say. `output_type` (an enum sensor,
    #: read by `OUTPUT_TYPE_PHASES`) overrides it when the plant shows it.
    grid_charge_phases: int = 1
    output_type: Match | None = None
    #: The plant's own grid import limit (kW): while it reads at most the fuse, and the person's on-site test showed
    #: it acts (`battery_control_import_limit_acts`), it bounds a grid charge with Home Assistant down.
    import_limit: Match | None = None
    note: str = ""
    #: Ways to hold, tried in order by the verification (`HoldVariant`); the first is in `states` and `peak_first`
    #: until another is chosen (`with_hold`). Empty: the brand has one hold.
    hold_variants: tuple[HoldVariant, ...] = ()
    #: The hold variant this control runs (`None`: the brand has one hold).
    variant: str | None = None
    #: Values written first, before the switch, on an enter after a hand-back whose write to a role that goes idle
    #: with the switch (`Role.idle_when`) was never read back: the plant may still hold the command it had, and these
    #: keep that command from acting until the role is written again (`executor.ExecState.unsure`).
    prime: Mapping[str, Native] = field(default_factory=dict)
    #: The plant's power target per phase, where the brand has one (`PhaseControl`); `None`: a grid charge is spread
    #: by the plant itself (`grid_charge_phases`).
    phase_control: PhaseControl | None = None
    #: Whether this control runs the targets per phase (`with_phases`): its roles, orders and states hold them.
    phased: bool = False
    #: Entities this control does not write but that must read a value, where they exist and are enabled, before it
    #: starts anything (`(role, value, reason)`): another control's targets left standing would act under it.
    watched: tuple[tuple[Role, Native, str], ...] = ()
    #: Writes a day per class (`WriteClass`), where they differ from `SETTING_WRITES`: only for a class the brand's
    #: own documents were read for (see the module).
    write_caps: Mapping[str, int] = field(default_factory=dict)
    #: Per action, the state as a function of the rate and of the house's net load now (kW, the site's import less
    #: the battery's charge), where the brand sets the **grid's** power and not the battery's: used instead of
    #: `states` when the load is known (`wanted`'s `house_kw`). An action in it does not run without that load.
    house_states: Mapping[str, Callable[[float, float], tuple[Want, ...]]] = field(default_factory=dict)
    #: How often the `watchdog_roles` are renewed (s); `None`: `WATCHDOG_RENEW_S`.
    watchdog_renew_s: int | None = None

    def role(self, name: str) -> Role:
        for role in (*self.roles, *(role for role, _value, _why in self.watched)):
            if role.name == name:
                return role
        raise KeyError(name)

    def write_cap(self, write_class: str) -> int:
        """The day's writes of a class: the brand's own figure, else a setting's (also for a class not checked)."""
        return self.write_caps.get(write_class, SETTING_WRITES)

    def variant_names(self) -> tuple[str, ...]:
        return tuple(variant.name for variant in self.hold_variants)

    def hold_idle(self) -> bool:
        """Whether this control's hold leaves the battery idle (`HoldVariant.idle`)."""
        return any(variant.idle for variant in self.hold_variants if variant.name == self.variant)

    def with_hold(self, name: str | None) -> BrandControl:
        """This control with the hold variant `name` (its writes and its peak cover); itself for `None`, for a name
        it does not know or for a brand with one hold. Only one variant is ever in use: the executor changes it only
        while it holds nothing."""
        variant = next((item for item in self.hold_variants if item.name == name), None)
        if variant is None or variant.name == self.variant:
            return self
        if self.phased:
            return self.with_phases(False).with_hold(name).with_phases(True)
        return replace(
            self, states={**self.states, ACTION_HOLD: variant.build},
            peak_first={**self.peak_first, ACTION_HOLD: variant.peak_first}, variant=variant.name,
        )

    def with_phases(self, on: bool) -> BrandControl:
        """This control running the brand's targets per phase (`PhaseControl`), or not; itself for a brand without
        them. The phased control has the targets (and what they require) among its roles and orders, writes them to
        0 in every action, and stops them first; a grid charge given a power per phase (`wanted`'s `phases`) writes
        the targets instead of the brand's own charge. The executor changes between the two only while it holds
        nothing (`Executor.use_phases`)."""
        spec = self.phase_control
        if spec is None or on == self.phased:
            return self
        if not on:
            base = next(control for control in CONTROLS if control.brand == self.brand)
            return base.with_hold(self.variant)
        idle = spec.idle()

        def with_idle(build: Callable[..., tuple[Want, ...]]) -> Callable[..., tuple[Want, ...]]:
            return lambda rate_kw, other: (*idle, *build(rate_kw, other))

        owned = {role.name for role in spec.owned()}
        return replace(
            self,
            roles=(*self.roles, *spec.owned()),
            states={action: with_idle(build) for action, build in self.states.items()},
            house_states={action: with_idle(build) for action, build in self.house_states.items()},
            watchdog_renew_s=spec.watchdog_renew_s if spec.watchdog_renew_s is not None else self.watchdog_renew_s,
            enter_order=spec.enter_order, restore_order=spec.restore_order,
            peak_first={**self.peak_first, ACTION_GRID_CHARGE: spec.stop_first},
            requires=(*self.requires, *((role.name, states, why) for role, states, why in spec.requires)),
            # What it owns it writes itself; nothing of them is left to watch.
            watched=tuple(row for row in self.watched if row[0].name not in owned),
            watchdog_roles=(*self.watchdog_roles, *spec.watchdog_roles),
            own_states={**self.own_states, **spec.own_states},
            phased=True,
        )

    def supports(self, action: str) -> bool:
        return action == ACTION_SELF_CONSUME or action in self.states

    def wanted(
        self, action: str, rate_kw: float, watchdog_s: int = WATCHDOG_S, phases: Phases | None = None, *,
        house_kw: float | None = None, behind_kw: Phases | None = None,
    ) -> dict[str, Native]:
        """Every restored role's wanted value for the action (`SNAPSHOT` where the action leaves it). `phases`: a
        grid charge per phase on a phased control (kW on L1, L2, L3, negative for a discharge), instead of the
        brand's own charge at `rate_kw`; `behind_kw`: the load behind each phase's point, for a `grid` target.
        `house_kw`: the house's net load now, for an action the brand runs by the grid's power (`house_states`)."""
        wanted: dict[str, Native] = {role.name: SNAPSHOT for role in self.roles if role.restored}
        if action != ACTION_SELF_CONSUME:
            spec = self.phase_control
            if phases is not None and self.phased and spec is not None and action == ACTION_GRID_CHARGE:
                wants = spec.wants(phases, behind_kw)
            elif house_kw is not None and action in self.house_states:
                wants = self.house_states[action](rate_kw, house_kw)
            else:
                wants = self.states[action](rate_kw, watchdog_s)
            for want in wants:
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


# TypQxQ `select.py` lines 105-114 (both pinned commits): the Remote EMS control mode's options, Appendix 6's 0-8.
_SIG_GRID_FIRST: Final = "Command Charging (Grid First)"  # 3
_SIG_PV_FIRST: Final = "Command Charging (PV First)"  # 4
_SIG_DISCHARGE_PV_FIRST: Final = "Command Discharging (PV First)"  # 5

#: The Remote EMS control modes each battery limit takes effect in, per protocol generation (`None`: every mode).
#: V2.7 plant parameters 23-24 (`sig27.txt` lines 1133-1145); V2.9 Table 5-2-1 rows 1-2 (`sig29.txt` 1120-1135).
SIGENERGY_LIMIT_MODES: Final[Mapping[str, Mapping[str, frozenset[str] | None]]] = {
    "V2.7": {
        "charge_limit": frozenset({_SIG_GRID_FIRST, _SIG_PV_FIRST}),
        "discharge_limit": frozenset({_SIG_DISCHARGE_PV_FIRST, "Command Discharging (ESS First)"}),
    },
    "V2.9": {"charge_limit": None, "discharge_limit": None},
}
#: Writes outside V2.7's scope, each on purpose: (action or `hold:<variant>`, limit). Acted on where the firmware
#: follows V2.9; ignored where it follows V2.7 (the hold's verification then fails and tries `pv_first`).
SIGENERGY_OUT_OF_SCOPE_V27: Final = frozenset(
    {("hold:msc_no_discharge", "discharge_limit"), (ACTION_DISCHARGE_HOUSE, "charge_limit")}
)


def _sig(mode: str, **limits: Callable[[float], Native]) -> Callable[[float, int], tuple[Want, ...]]:
    def build(rate_kw: float, _watchdog_s: int) -> tuple[Want, ...]:
        wants = [Want(name, fn(rate_kw)) for name, fn in limits.items()]
        return (*wants, Want("remote_ems", True), Want("control_mode", mode))

    return build


#: Sigenergy's holds, in the order the verification tries them.
SIGENERGY_HOLDS: Final = (
    # The discharge limit at 0 in Maximum Self Consumption: acts on V2.9-era firmware (and on SPC117 with Remote EMS
    # on, TypQxQ discussion #424). The peak cover lifts the limit; the plant balances each phase itself in MSC.
    HoldVariant(
        "msc_no_discharge", _sig(_SIG_MSC, discharge_limit=lambda _rate: 0.0), ("discharge_limit",),
        "Maximum Self Consumption with the discharge limit at 0",
    ),
    # For firmware that follows V2.7 (no limit acts in mode 2): mode 4 with the charge limit at 0, where that limit
    # acts; nothing charges or discharges, a sun surplus is exported. The peak cover writes the mode back to Maximum
    # Self Consumption.
    HoldVariant(
        "pv_first", _sig(_SIG_PV_FIRST, charge_limit=lambda _rate: 0.0), ("control_mode",),
        "Command Charging (PV First) with the charge limit at 0", idle=True,
    ),
)

_SIG_PCS: Final = "PCS Remote Control"  # 0: the plant follows its active power targets
_SIG_PHASE_TARGETS: Final = (
    Role("phase_l1_target", "number", _key("plant_phase_a_active_power_fixed_target", "number"), step=0.01,
         restored=False, write_class="setpoint"),
    Role("phase_l2_target", "number", _key("plant_phase_b_active_power_fixed_target", "number"), step=0.01,
         restored=False, write_class="setpoint"),
    Role("phase_l3_target", "number", _key("plant_phase_c_active_power_fixed_target", "number"), step=0.01,
         restored=False, write_class="setpoint"),
)
# 40001, the plant's total active power target: acts in PCS Remote Control as the phase targets do.
_SIG_TOTAL_TARGET: Final = Role(
    "power_target", "number", _key("plant_active_power_fixed_target", "number"), step=0.01, restored=False,
    write_class="setpoint",
)
_SIG_PHASE_SWITCH: Final = Role(
    "phase_control", "switch", _key("plant_independent_phase_power_control_enable", "switch"), restored=False
)
#: Sigenergy's targets per phase (see the module): phase A, B, C are L1, L2, L3; a negative target charges. The
#: independent phase switch (40030) is read, never written: it changes how the plant shares its power in the
#: person's own modes too, so SpotNav runs per phase only where the person has it on. The targets are written while
#: Remote EMS is still off (they are available then), so the mode finds them in place; a hand-back writes them to 0,
#: then the mode to Maximum Self Consumption, then Remote EMS off. The battery limits are left as found.
SIGENERGY_PHASES: Final = PhaseControl(
    targets=_SIG_PHASE_TARGETS,
    charge_sign=-1.0,
    follow=(Want("remote_ems", True), Want("control_mode", _SIG_PCS)),
    target_kind="plant",
    zero=(_SIG_TOTAL_TARGET,),
    requires=((_SIG_PHASE_SWITCH, frozenset({"on"}), "phase_control_off"),),
    enter_order=(
        "power_target", "charge_limit", "discharge_limit", "phase_l1_target", "phase_l2_target", "phase_l3_target",
        "remote_ems", "control_mode",
    ),
    restore_order=(
        "phase_l1_target", "phase_l2_target", "phase_l3_target", "power_target", "control_mode", "remote_ems",
        "charge_limit", "discharge_limit",
    ),
    stop_first=("phase_l1_target", "phase_l2_target", "phase_l3_target", "power_target", "control_mode"),
)

SIGENERGY_CONTROL: Final = BrandControl(
    brand="sigenergy",
    adapter=SIGENERGY,
    roles=(_SIG_ENABLE, _SIG_MODE, _SIG_CHARGE, _SIG_DISCHARGE),
    states={
        ACTION_HOLD: SIGENERGY_HOLDS[0].build,
        ACTION_GRID_CHARGE: _sig(_SIG_GRID_FIRST, charge_limit=lambda rate: native(_SIG_CHARGE, rate)),
        ACTION_DISCHARGE_HOUSE: _sig(_SIG_MSC, charge_limit=lambda _rate: 0.0),
        ACTION_EXPORT_DISCHARGE: _sig(
            _SIG_DISCHARGE_PV_FIRST, discharge_limit=lambda rate: native(_SIG_DISCHARGE, rate)
        ),
    },
    enter_order=("charge_limit", "discharge_limit", "remote_ems", "control_mode"),
    restore_order=("control_mode", "remote_ems", "charge_limit", "discharge_limit"),
    own_states={"EMS work mode": frozenset({"remote_ems"})},
    # AI mode (owner 2026-11-16): taken over only after the person acknowledged that turning control off leaves the
    # plant in Maximum Self Consumption (Remote EMS off lands there), not in AI mode.
    takeover={"EMS work mode": frozenset({"ai_mode"})},
    requires=(("remote_ems", frozenset({"off"}), "remote_ems_already_on"),),
    # A hold's peak cover is its variant's (`SIGENERGY_HOLDS`). A grid charge stops with the mode back at Maximum
    # Self Consumption.
    peak_first={ACTION_HOLD: SIGENERGY_HOLDS[0].peak_first, ACTION_GRID_CHARGE: ("control_mode",)},
    # A three-phase SigenStor charges from the grid balanced over its phases; a single-phase one says so in its
    # inverter's Output Type (TypQxQ 2.0.3 `static_sensor.py` key `inverter_output_type`, register 31004, disabled
    # by default). Grid Import Limitation: `number.py` key `plant_grid_maximum_import_limitation` (lines 201-216),
    # register 40040 (`modbusregisterdefinitions.py` lines 1633-1641), kW.
    grid_charge_phases=3,
    output_type=_key("inverter_output_type"),
    import_limit=_key("plant_grid_maximum_import_limitation", "number"),
    note="Remote EMS replaces the app's mode only while it is on; SpotNav takes a plant in Maximum Self"
    " Consumption only, so turning it off hands back the mode it found.",
    hold_variants=SIGENERGY_HOLDS,
    variant=SIGENERGY_HOLDS[0].name,
    # 40031 keeps its last value while Remote EMS is off, and the mode select is unavailable then (it cannot be
    # written before the switch): both limits at 0 make a Command Charging or Discharging mode left there do nothing
    # (V2.7: 40032 acts in 3 and 4, 40034 in 5 and 6; V2.9: everywhere) until the mode is written.
    prime={"charge_limit": 0.0, "discharge_limit": 0.0},
    phase_control=SIGENERGY_PHASES,
    # A target left standing would run in the seconds of PCS Remote Control a Remote EMS on passes through.
    watched=tuple((role, 0.0, "stale_target") for role in (_SIG_TOTAL_TARGET, *_SIG_PHASE_TARGETS)),
    # The protocol is silent on how often the dispatch registers may be written (see the module): the moderate cap.
    write_caps={"setpoint": SETPOINT_WRITES_UNSTATED},
)

#: An inverter's Output Type (Sigenergy's enum, lower-cased) as the phases it feeds.
OUTPUT_TYPE_PHASES: Final[Mapping[str, int]] = {"l_n": 1, "l1_l2_n": 2, "l1_l2_l3": 3, "l1_l2_l3_n": 3}

# The normal state has no wanted value for these: the switch goes off, the select to MSC (only while it can).
# The targets per phase go back to 0 (only a phased control has their entities).
_SIGENERGY_NORMAL: Final = {
    "remote_ems": False, "control_mode": _SIG_MSC,
    **{role.name: 0.0 for role in (_SIG_TOTAL_TARGET, *_SIG_PHASE_TARGETS)},
}

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

# --- Victron ESS (see the module) ---------------------------------------------------------------------------------

# -1 is "no limit" and 0 is "none at all", one watt apart on entities whose step is 50: read to the watt.
_VIC_CHARGE = Role(
    "charge_power", "number", _key("system_ess_max_charge_power", "number"), scale=1000.0, step=50.0, tolerance=1.0
)
_VIC_DISCHARGE = Role(
    "discharge_power", "number", _key("system_ess_max_inverter_power_limit", "number"), scale=1000.0, step=50.0,
    tolerance=1.0,
)
_VIC_GRID = Role(
    "grid_setpoint", "number", _key("system_ac_power_set_point", "number"), scale=1000.0, step=10.0, tolerance=1.0
)
_VIC_MODE = Role("ess_mode", "select", _key("system_ess_mode", "select"))
_VIC_NO_CHARGE = Role("disable_charge", "switch", _key("vebus_hub4_disable_charge", "switch"), restored=False)
_VIC_NO_FEED_IN = Role("disable_feed_in", "switch", _key("vebus_hub4_disable_feed_in", "switch"), restored=False)
_VIC_EXTERNAL: Final = "external_control"  # Hub4Mode 3
#: The Multi falls back to passthru this long after the last write of a setpoint (s), and how often each is renewed.
VICTRON_SILENCE_S: Final = 60
VICTRON_RENEW_S: Final = 20
#: The range of the setpoint entities (kW): `vebus_ac_power_setpoint_{phase}` is -10000 to 10000 W, and the grid
#: setpoint's is the GX's own, 10000 W where the GX does not state it (victron-mqtt `_victron_topics.py`).
VICTRON_SETPOINT_MAX_KW: Final = 10.0


def _vic_grid(kw: float) -> float:
    """The grid setpoint (W) for a site power of `kw` (+ import), its size rounded down to the role's step and cut
    to the entity's range (the battery then takes or gives less than its rate, never more)."""
    size = native(_VIC_GRID, min(abs(kw), VICTRON_SETPOINT_MAX_KW))
    return 0.0 if size == 0 else round(math.copysign(size, kw), 6)


def _vic_charge(rate_kw: float, house_kw: float) -> tuple[Want, ...]:
    # The charge power bounds the battery's draw; the grid setpoint, the house now plus the rate, drives it.
    return (Want("charge_power", native(_VIC_CHARGE, rate_kw)), Want("grid_setpoint", _vic_grid(house_kw + rate_kw)))


def _vic_export(rate_kw: float, house_kw: float) -> tuple[Want, ...]:
    return (Want("grid_setpoint", _vic_grid(house_kw - rate_kw)),)


def _vic_phase_roles(name: str, key: str, method: RoleMethod = "sensor", **kwargs: Any) -> tuple[Role, Role, Role]:
    """A role per phase (W, never restored): `phase_l<n>_<name>` on the short id `<key>_l<n>`."""
    roles = [
        Role(f"phase_l{n}_{name}", method, _key(f"{key}_l{n}", method), scale=1000.0, restored=False, **kwargs)
        for n in (1, 2, 3)
    ]
    return (roles[0], roles[1], roles[2])


_VIC_PHASE_TARGETS: Final = _vic_phase_roles(
    "target", "vebus_ac_power_setpoint", "number", step=10.0, write_class="setpoint"
)
#: Victron's Mode 3 (see the module): a setpoint per phase is the power on that Multi's AC input, + from the grid,
#: so a charge is written as the load on its AC output plus the charge. The Multis follow them only in external
#: control, where SpotNav owns them and both flags; in every other state the GX writes them itself, so they are
#: neither parked nor handed back, and the one write that stops a charge per phase on all three phases is the ESS
#: mode back as found. Each setpoint is renewed well inside the Multi's 60 s watchdog; when the writes stop, the
#: Multi goes to passthru by itself.
VICTRON_PHASES: Final = PhaseControl(
    targets=_VIC_PHASE_TARGETS,
    charge_sign=1.0,
    follow=(Want("ess_mode", _VIC_EXTERNAL), Want("disable_charge", False), Want("disable_feed_in", False)),
    target_kind="grid",
    roles=(_VIC_MODE, _VIC_NO_CHARGE, _VIC_NO_FEED_IN),
    park=False,
    behind=_vic_phase_roles("load", "vebus_inverter_output_power"),
    point=_vic_phase_roles("input", "vebus_inverter_input_power"),
    watchdog_roles=tuple(role.name for role in _VIC_PHASE_TARGETS),
    watchdog_renew_s=VICTRON_RENEW_S,
    silence_fallback_s=VICTRON_SILENCE_S,
    three_phase_by_targets=True,
    limit_kw=VICTRON_SETPOINT_MAX_KW,
    own_states={"ESS mode": frozenset({_VIC_EXTERNAL})},
    enter_order=(
        "charge_power", "discharge_power", "grid_setpoint", "ess_mode", "disable_charge", "disable_feed_in",
        "phase_l1_target", "phase_l2_target", "phase_l3_target",
    ),
    restore_order=("ess_mode", "grid_setpoint", "charge_power", "discharge_power"),
    stop_first=("ess_mode",),
)

VICTRON_CONTROL: Final = BrandControl(
    brand="victron",
    adapter=VICTRON,
    roles=(_VIC_CHARGE, _VIC_DISCHARGE, _VIC_GRID),
    states={
        ACTION_HOLD: lambda _rate, _w: (Want("discharge_power", 0.0),),
        ACTION_GRID_CHARGE: lambda rate, _w: _vic_charge(rate, 0.0),
        ACTION_DISCHARGE_HOUSE: lambda _rate, _w: (Want("charge_power", 0.0),),
        ACTION_EXPORT_DISCHARGE: lambda rate, _w: _vic_export(rate, 0.0),
    },
    house_states={ACTION_GRID_CHARGE: _vic_charge, ACTION_EXPORT_DISCHARGE: _vic_export},
    # The charge power is in place before the setpoint asks for import; back: the setpoint first, so the limit is
    # never loosened while the plant is still asked to import.
    enter_order=("charge_power", "discharge_power", "grid_setpoint"),
    restore_order=("grid_setpoint", "charge_power", "discharge_power"),
    peak_first={ACTION_HOLD: ("discharge_power",), ACTION_GRID_CHARGE: ("grid_setpoint",)},
    # The entities do not say how many phases the Multis are on: the balanced charge is capped as if on one.
    grid_charge_phases=1,
    note="Mode 2 for a hold, a balanced charge and a discharge (settings on the GX, written rarely); external"
    " control with a setpoint per phase, renewed inside the Multi's 60 s watchdog, for a charge per phase. Not"
    " tested on a plant.",
    phase_control=VICTRON_PHASES,
    write_caps={"setpoint": SETPOINT_WRITES_WATCHED},
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
    takeover={"EMS work mode": frozenset({"ai_mode"})},
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

CONTROLS: Final[tuple[BrandControl, ...]] = (
    SIGENERGY_CONTROL, SOLAREDGE_CONTROL, HUAWEI_CONTROL, SOLAX_CONTROL, VICTRON_CONTROL,
)


#: The export-limit controls: only where the brand's adapter already knows the entity (`EXPORT_LIMIT_TABLE` lists
#: the brands seen with a local export limit, mapped or not).
EXPORT_CONTROLS: Final[tuple[BrandControl, ...]] = (SIGENERGY_EXPORT_CONTROL,)


def control_for(brand: str | None, variant: str | None = None) -> BrandControl | None:
    """The brand's control (with its hold variant `variant`, the first when `None` or unknown), `None` for a brand
    SpotNav writes nothing to."""
    control = next((control for control in CONTROLS if control.brand == brand), None)
    return None if control is None else control.with_hold(variant)


def phase_table() -> list[dict[str, Any]]:
    """The brands with targets per phase and what a charge of 1 kW on each phase writes, for diagnostics."""
    rows: list[dict[str, Any]] = []
    for control in CONTROLS:
        spec = control.phase_control
        if spec is not None:
            rows.append({
                "brand": control.brand, "targets": list(spec.names()),
                "writes": {want.role: want.value for want in spec.wants((1.0, 1.0, 1.0))},
                "requires": {role.name: sorted(states) for role, states, _why in spec.requires},
            })
    return rows


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
            row = {"brand": control.brand, "action": action, "writes": {w.role: w.value for w in wants}}
            if action == ACTION_HOLD and control.hold_variants:
                row["variants"] = {
                    variant.name: {w.role: w.value for w in variant.build(5.0, WATCHDOG_S)}
                    for variant in control.hold_variants
                }
            rows.append(row)
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
