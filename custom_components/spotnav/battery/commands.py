"""What SpotNav writes to a home battery for each of the plan's actions, per brand (behind the site's gate,
`gate.py`).

Each brand's control is a set of **roles** (the entities it writes or reads back) and, per plan action, the
**state** those roles must be in: a value per role, or `SNAPSHOT` for "the value found before SpotNav first wrote".
"Normal" (the plan's `self_consume`, and every restore) is the state with every written role back at its snapshot.
The executor (`executor/`) writes only the roles whose reading differs from the wanted state, one at a time and in
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
  (mode 5, discharge limit) keep to both generations. The hold (`SIGENERGY_HOLDS`) is `pv_first`, mode 4 with the
  charge limit at 0, the only one that keeps the plant's own peak cover under the import limit (40040) with Home
  Assistant down (on site 2026-11-23; `msc_no_discharge`, Maximum Self Consumption with the discharge limit at 0,
  failed it and is no longer used). Appendix 6 names mode 4 "Command charging (consume PV power first)" (`sig27.txt`
  lines 2180-2183, `sig29.txt` line 3346): a charge at the charge limit, from the sun first and then the grid (haeo's
  Sigenergy walkthrough and Roving-Ronin's EMS-Events-Card use it so). At a limit of 0 it charges nothing, and the
  discharge limit does not act in it under V2.7 (it acts only in 5 and 6) while the plant discharges in no Command
  Charging mode: the battery stands idle and a sun surplus is exported. That is within V2.7's scope and V2.9's, and it
  is also what a command left standing after Home Assistant died does. Its peak cover writes the mode back to Maximum
  Self Consumption, where the plant balances each phase itself. A `pv_first` hold whose battery charges anyway is
  handed back (`executor.HOLD_CHARGE_S`) and fails its verification. A discharge to the house has no mode in V2.7
  ("cover the house, store nothing"): it stays Maximum Self Consumption with the charge limit at 0, valid under V2.9
  and at worst ignored under V2.7 (the battery then also stores a sun surplus, as in self-consumption). V2.8 removed
  41500 (Remote EMS dispatch enable, never used here); V2.9 added 0x08 V2G to 40031. 40049, Active power regulation
  gradient (%/s of the rated power, V2.8+), bounds how fast the plant changes its output and so its reaction to a mode
  or limit change; SpotNav never writes it. A plant is taken in Maximum Self Consumption (`adapters.SIGENERGY`'s
  mode rule), or in AI mode once the
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
  watchdog: when SpotNav stops renewing the command the inverter falls back to its default mode by itself. It is
  written as `WATCHDOG_S` (30 minutes) and written again, with the command mode, every `WATCHDOG_RENEW_S` (half of
  it) and at every change of action, so it is renewed whether the inverter restarts its timer on the timeout's write
  or on the command's. A grid charge needs an AC charge policy other than Disabled (`ac_charge_policy`).
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
* **Solinteg** (MHT and M2HT hybrids; M-TEC Energy Butler and Wattsonic share the register map), `solax_modbus`'s
  Solinteg plugin, wills106 `4acd644e82c1405ea4aa99b678de239d55f2aaae` (`plugin_solinteg.py`, read 2026-11-18;
  `plans/research_integrations/solinteg_m2ht_2026-11.md` has the field notes). **Never run on a device.** Every key
  below is a real Modbus register written at once (`WRITE_SINGLE_MODBUS`, the plugin's default), not a local value:
  select `working_mode` (register 50000: "General" 0x101, "Economic", "UPS", "PeakShift", "Feed-In", "Off-Grid",
  "EMS ACCtrl" 0x301, "EMS General", "EMS BattCtrl" 0x303, "EMS Off-Grid", "ToU") and number
  `battery_charge_discharge_power_target` (50207, S16 x 0.01, kW, -200 to 200, step 0.01; **negative charges**,
  positive discharges: the battery's own power, not the AC side, and acted on only in EMS BattCtrl). A hold is EMS
  BattCtrl with the target at 0 (the battery stands idle and stores no sun: `HoldVariant.idle`), a grid charge the
  target at minus the rate, a discharge to the grid the target at the rate. There is no "cover the house, store
  nothing" among the EMS modes, so a discharge to the house is the plant's own General mode (it also stores a sun
  surplus, as Sigenergy's does under V2.7). The hand-back writes the mode to General, which releases the battery in
  one write, then the target to 0, so that nothing is left for the next one who enters EMS BattCtrl; and the control
  starts nothing while the target reads other than 0 (`watched`): the mode is written before the target (an owner
  found values written before the mode switch read back but not act), so a target left standing would run for the
  seconds between the two. The inverter's own AC caps in EMS BattCtrl (`ems_battctrl_max_ac_power_limit` /
  `ems_battctrl_min_ac_power_limit`, 50208/50209) and its output priority (50210) are left as found: they bound the
  inverter's own AC power, not the site's import, and are no guard for the fuse. A plant is taken only in General
  (`adapters.SOLINTEG`'s mode rule), so the hand-back lands in the mode it was found in.
  **No dead-man.** No public Solinteg protocol has a timeout for an EMS command and an owner measured a target
  standing for 16 minutes with no writes: a command stays if Home Assistant stops, as Sigenergy's does, so a grid
  charge is always under the HA-down cap (`gate.grid_charge_cap_kw`). The site import limit (`import_limit_switch`
  / `import_limit_value`, 50007/50009) and the peak shaving settings (50016-50018, 50022) are never counted on and
  never written: whether they act in an EMS mode is not known, the cloud has been seen to write limits back, and they
  are settings kept by the inverter (`BrandControl.import_limit` stays empty). How the inverter spreads a BattCtrl
  charge over its phases is not stated either, so the cap takes it as all on one phase (`grid_charge_phases` 1).
  **Per phase** (`SOLINTEG_PHASES`): in "EMS ACCtrl" with select `ems_acctrl_mode` (50202) at "Power Setting on each
  Phase" the inverter follows numbers `ems_acctrl_phase_{a,b,c}_power` (50204-50206, S16 x 0.01, kW, -200 to 200,
  disabled by default): the inverter's AC power on that phase, the sun included (the battery takes the difference),
  so a target is the plant's own power (`target_kind` `plant`), negative taking from the grid by the protocol's
  sign for AC power (v00.02: the AC limits 50208/50209, where negative is import; not confirmed on a device, the
  verification's mode `per_phase` checks it). It needs select `grid_unbalanced_output` (50006) "on", which is read
  and never written (it changes the plant's own modes too). The targets are written while the plant is still in its
  former mode, then the scheduling mode, then the working mode, so the mode finds them in place; whether targets
  written before the switch act is the same open question as for the limits above (a charge they do not act for is
  handed back as one without effect). The scheduling mode is left as written: it acts only in EMS ACCtrl. **The
  plugin has no signed power per phase of the inverter** (its "Inverter Current L1-3" are unsigned currents, its
  "Meter Active Power L1-3" the grid's), so `adapters.SOLINTEG` has no `plant_phase_power` and a charge per phase
  cannot be verified or judged: the recipe is data until the plugin reads one.
  **The dongle.** The LAN/WiFi dongle takes one Modbus TCP client and stops answering for 4-8 s about once a minute
  while it uploads to the cloud (longer when the cloud is slow; a register it does not know has wedged it for about
  70 s). The plugin answers a write only once the inverter acknowledged it, raises when it did not (a write not
  taken, as for any brand), and then shows the written value at once; the register itself is read at its next poll,
  where a value the inverter or the cloud changed back shows as a change by someone else. A read-back may therefore
  arrive a stall late: `READBACK_STALL_S` instead of the ledger's 60 s for the writes that start or change a
  command (a hand-back or a peak cover writes General again after the 60 s: the house is on the grid meanwhile).
  **Writes a day:** no document says whether 50207 and 50204-50206 are kept in flash; owners write 50207 every
  second, but that is no maker's word, so they are setpoints at `SETPOINT_WRITES_UNSTATED` (500) and the modes are settings (96).
* **Export limit** (`EXPORT_CONTROLS`, the action `ACTION_CURTAIL_EXPORT`: no export while the sell price is below
  zero, `export_curtail.py`): its own control beside the battery's, written by the same executor and ledger. Sigenergy:
  number `plant_grid_point_maximum_export_limitation` ("Grid Export Limitation", register 40038, U32, gain 1000, kW
  0-100, step 0.001, disabled by default; TypQxQ/Sigenergy-Local-Modbus `41182ec23b9d146ab5c01379a7f08994aaada75d`
  (2.0.3) `number.py` lines 185-199 (the key at 186), `modbusregisterdefinitions.py` lines 1624-1632). It applies in every EMS mode
  (the integration's `docs/MODBUS_ENTITIES.md`, "Grid export power limit": "All EMS modes", "Hybrid / PV · grid
  sensor"), so it does not need Remote EMS, but it needs the plant's grid sensor. Its never-set all-ones value reads
  `unknown` as the battery limits do; the integration has no entity that sets it so again, and the plant's maximum is
  never written in its place (it could overwrite a grid operator's cap), so a plant whose limit was never set is not
  curtailed (`no_restore_value:export_limit`). Five more brands have one since 2026-11-25 (`EXPORT_CONTROLS`, the
  recipes near the end of this file; every entity read in its source at the commit `EXPORT_LIMIT_TABLE` names):
  Victron (`system_ess_max_feed_in_power`, -1 no limit, `Role.no_limit`), Sungrow (`uid_export_power_limit` with the
  switch `sg_export_power_limit_switch`), Solis (`solis_modbus_inverter_backflow_power` with the switch `43073_4`),
  GoodWe (`grid_export_limit`, with the HACS fork's switch `grid_export_limit_switch` where it exists) and SolaX
  (`export_control_user_limit`). Each is one number at the grid point (`BrandControl.limit_point`: a cap on the
  inverter's AC output is never an export control) with at most one switch that enables it: the value is written
  first and the switch last, and back the switch first; a switch found off is switched off again and the value
  handed back as found; only SpotNav's own values are ever written over (`restore_own_only`). None of the five has
  run on a device: they write on a beta tester's site only (`export_field_tested`), and where no source states that
  0 means no export (`zero_stated`) a check on the plant comes first (`control`'s export check).
* **GoodWe**, `goodwe`, the HACS fork mletenay/home-assistant-goodwe-inverter `e6e9b385dd0000b3e6dcf3742c61c38e0dc75c8f`
  (0.9.9.30; library goodwe 0.4.10 `66dfcdd9cd947bfd799f2bddb40b4e05ba9db6f6`; research
  `goodwe_tesla_enphase_control_2026-11.md`). **Core `goodwe` is not enough**: it has no `eco_charge` option and no eco
  power or SoC number, so with the fork's numbers missing the status says `needs_hacs_integration`. Entities (unique
  id `goodwe-<key>-<serial>`, both integrations): select `operation_mode` (fork options general, off_grid, backup, eco,
  peak_shaving, self_use, eco_charge, eco_discharge), numbers `eco_mode_power` (% of the battery's rated power, 0-100)
  and `eco_mode_soc` (%, 0-100) and `battery_discharge_depth` (on-grid depth of discharge, %, 0-99; the register holds
  100 less it, `et.py` `set_ongrid_battery_dod`). The two eco numbers are **not written to the inverter on their
  own** (`number.py`, `setter=None`): the select caches them and sends the eco group (`set_operation_mode`: group
  1, 00:00-23:59, all days, groups 2-4 off, work mode 3) when it is set to `eco_charge`, and again whenever either
  number's state changes while it is in `eco_charge` (`select.py` `update_eco_mode_power/soc`, a state-change listener
  the select sets up for the numbers registered when it is), so a lowered rate reaches the inverter by the number's
  write alone. So numbers first, the mode last; back: the mode first, then the numbers. The select does not poll
  (`should_poll = False`) and the numbers only echo what was written, so the read-back proves only what Home Assistant
  holds (`local_readback`): the battery's power shows the effect. A grid charge is `eco_charge` at a share of the
  rated power (1 % steps, rounded down; the rating is the BMS' present limit times the voltage, an estimate) with the
  SoC ceiling at 100. **The ceiling is not trusted**: two firmwares ignore it (HACS #466, #486), so the charge ends with
  the plan's slot, not at a SoC. A discharge for the house is plain `general` (self-consumption covers it). **Hold** is
  the depth of discharge: the cut-off is set to the SoC now (depth = 100 less floor(SoC)), so the battery stops
  discharging and, with the cut-off not above the SoC, has no reason to charge. **Unverified**: the library says
  nothing of what the inverter does at a cut-off equal to its SoC; the other candidates read back as `general`
  (an eco group at 0 % is what the select shows as general, HACS #229), so they could not be told from no hold.
  There is no dead-man: eco group, mode and depth are inverter settings that survive Home Assistant stopping, so a
  grid charge is capped for that case as for Sigenergy (`grid_charge_phases` is not known: 1). EEPROM wear is not
  documented (assumed): 96 setting writes a day and no peak cover.
* **Tesla Powerwall**, `tesla_fleet` / `teslemetry` (core `3df93331719fcb206ab24c165cadf1e796d52c02`, tesla-fleet-api
  1.23.1), a **slow** cloud control (`BrandControl.slow`). Unique id `<energy site id>-<key>`: number
  `backup_reserve_percent` (%, 0-100, step 1), switch `components_disallow_charge_from_grid_with_solar_installed`
  (**inverted**: on = grid charging allowed; it exists only on a site with battery and solar, so it is optional here)
  and the mode select `default_real_mode` (autonomous, backup, self_consumption), which is only read: the control runs
  in `self_consumption` as found and never writes it (`autonomous` is Time-Based Control, the person's optimiser; `backup`
  pins the reserve). A hold turns grid charging off (where the switch exists; restored on the hand-back) and sets the
  reserve to floor(SoC now) less 2 points (`soc_floor_margin_pct`), never above the SoC: a reserve above the SoC
  charges from the grid, and the cloud's SoC may be a minute old while the battery discharges. A grid charge is the reserve one block ahead of the level with grid charging allowed (`soc_ceiling`,
  `charge_block_s`: 30 minutes at the recipe's rate, renewed each block. There is no dead-man, so a Home Assistant or a
  cloud that stops leaves at most one block's charge from the grid and the reserve there, never 100 %); **its power is
  not controllable** (about
  1.7 kW per Powerwall in field reports, not measured here), so the gate and the executor use the plant's own
  maximum as the rate (`BrandControl.fixed_charge`, `fixed_rate_kw`: the larger of what is read or learnt and 5 kW per
  13.5 kWh) and **refuse** the charge when it does not fit under the fuse cap, instead of lowering it. Slow means: planned actions only
  (no hold for a car, no peak cover, no change of action sooner than `min_change_s`), read-back waits `readback_s`
  (the cloud answers in minutes and tesla_fleet's entities echo the value before the cloud does, `local_readback`), a
  day's writes are capped at 24 (Tesla's real per-day limit for energy settings is unconfirmed and a figure of 5 a day
  exists), and the hand-back writes the reserve first (it stops a forced charge at once). No dead-man: the reserve
  and the switch persist in the cloud and on the gateway.
* **Ferroamp**, `ferroamp`, henricm/ha-ferroamp `b18097f914abdc30d33beef0da1bdfd79674130f` (1.18.0, read 2026-11-18;
  the ExtApi spec is Rev E, 2021-03): services `ferroamp.charge` and `ferroamp.discharge` (`power` W, the whole
  system's, not per battery or phase; the service form's slider is 500-30000 W in steps of 500) and
  `ferroamp.autocharge` (hands the batteries back to the hub), each with `target`, the EnergyHub's device id
  (`__init__.py`, `services.yaml`). They publish to `extapi/control/request` and read nothing back; the sensor
  `<slug>_ehub_last_cmd` (`sensor.py`, state `charge (1000)`, `discharge (500)` or `auto`, kept over a restart)
  shows the last request on the topic, whoever sent it, set when the request is published; its attributes
  `transId` and `status` (`add_request`, `add_response`) carry the hub's `ack` or `nak` for that request. The state
  alone proves only that a request was published, so the role reads it only once the attributes show the hub's
  `ack` (`Role.answer`, `answered`): until then, and after a `nak`, it is no reading, so a write is confirmed by the
  hub's answer and a refused one (a hand-back to `auto` among them) is retried or handed back like any write that
  does not read back. The attributes are not restored over a restart: with no `transId` the state is read as it is.
  The battery's power judges the effect besides (`local_readback`). The spec documents no timeout for a request (nothing renews it, no watchdog is mapped) and
  no value that holds: a hold is `charge` at 0 W, `discharge` at 0 W the second variant for the verification to try
  (not verified on a hub; the hub may nak a 0). One request at a time: a new one while another runs is nak'd (no error
  to the service call; the `nak` is what shows it). A discharge to the house is `auto`, the hub's own
  strategy (`discharge` at a power may export); a person who has FerroAI or a peak-shaving strategy on in EnergyCloud
  gets that strategy there. That strategy cannot be read (the adapter has no mode rule), so the control waits for
  the person's acknowledgement that SpotNav's requests override it (`strategy_unread`). **ACE** (adaptive current equalisation) moves
  energy between the phases in the hub to keep each grid phase under the threshold set in EnergyCloud; it runs in
  the hub, but its reach (20 A per hub) and whether it acts on the batteries' own grid charge are not documented, nor is
  how the hub spreads a total over its three phases, so the grid charge cap counts the charge on one phase
  (`grid_charge_phases` 1) until a hub has shown an even split, and does not count on ACE.
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
    the setpoint is written only when the rate changes, never to follow the house. A grid setpoint found below
    `VICTRON_SALE_BELOW_W` (a sale running when the snapshot was taken) is handed back as the GX's default 50 W, never
    as found (`Role.restored_below`); a smaller negative bias (-20 W, so the house never imports) is the person's and
    is handed back as found. While it reads below that and SpotNav holds no snapshot of it, the checklist says so
    (`grid_setpoint_selling`): a hold or a discharge for the house does not write it, so the sale would go on.
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
    So the three setpoints are renewed every `VICTRON_RENEW_S` (each is then written again within about 30 s). By
    the page a charge per phase then stops by itself within `VICTRON_SILENCE_S` of the last write, but that is
    quoted, not seen: a firmware where the rule does not act, or something else on the GX that republishes the
    setpoints (Node-RED, a script), would leave them standing. So the recipe's dead-man is only stated, not
    measured (`PhaseControl.expiry`, `gate.expiry_lifts`): each phase's HA-down cap is reckoned with the house at its learned
    peak, as Sigenergy's (`gate.grid_charge_phase_caps_kw`), and bounds the whole target that would stand (the load
    behind the point plus the charge, `executor._phase_room_kw`). **Before it counts as measured** a field test on the
    plant, per Venus OS version (the firmware key), must show: (1) a charge per phase of at least 1 kW on each
    phase, SpotNav stopped mid-charge (Home Assistant stopped, not a hand-back), the ESS mode still
    `external_control`; (2) within 70 s of the last write each Multi's AC input power falls to its AC output's load
    (the battery's power per phase about 0) and stays there for 5 minutes, with the setpoints still reading what was
    last written; (3) the same with a renewal running for 10 minutes first, the charge not interrupted meanwhile
    (an unchanged value written again does renew the timer); (4) nothing else on the GX writes
    `/Hub4/L1..L3/AcPowerSetpoint` (no Node-RED flow, Dynamic ESS off). After such a stop the plant stays in
    passthru (the ESS mode is a setting) until Home Assistant is back and hands the mode back: it then neither
    stores the sun nor covers the house. Both flags are written off for the charge (with feed-in allowed the
    battery covers a load that passes the setpoint, instead of the grid). The setpoints are not stored in flash
    (the GX's own loop writes them all the time in its modes) and are made for a control loop:
    `SETPOINT_WRITES_WATCHED` a day.
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
* **FoxESS**, `foxess_modbus`, nathanmarlor/foxess_modbus `94252f0` (read 2026-11-20 and 2026-11-24,
  `plans/v3_foxess.md`). **Never run on a device; limits only.** Numbers `max_charge_current` and
  `max_discharge_current` (A, 0-50, step 0.1; holding 41007/41008 on H1, KH and H3, 46607/46608 on H3-Pro,
  H3-Smart, EVO and 1KOMMA5's `1K5-HI`, `entities/entity_descriptions.py` `_configuration_entities`; a sensor of the
  same key exists beside each and is not a role). Each write goes to the inverter at once (`modbus_number.py`
  `async_set_native_value`, which raises on a Modbus error); the entity shows the value written for a few seconds
  (`written_value`) and then the register as polled, so a value the inverter refused shows at the next poll as a
  change by someone else. A **hold** is the discharge current at 0 (the battery covers nothing; a sun surplus is still
  stored), a **discharge for the house** the charge current at 0 (it covers the house and stores nothing). Both are
  only lower limits in the work mode the owner runs ("Self Use", `adapters.FOXESS`'s mode rule: a plant found in any
  other mode is someone else's), and a limit left standing never adds import, so if Home Assistant stops the battery
  only covers less: no fuse cap is needed and nothing expires (`NO_EXPIRY`). The hand-back writes both back as found.
  **No grid charge and no sale.** The integration's only way to charge from the grid on H3, H3-Pro and 1K5 is its
  remote control (the work mode select's "Force Charge" / "Force Discharge" with the numbers `force_charge_power` /
  `force_discharge_power`, `remote_control_manager.py`). It renews the inverter's remote control on every poll and
  the inverter drops it 2 x poll rate after the last renewal, but before enabling it the integration writes a
  fallback work mode, **Back-up for a charge** (it charges as hard as it can) and Feed-in First for a discharge, and
  after a restart it never writes the mode back (`_disable_remote_control` with no mode, "let the watchdog catch it"):
  a Home Assistant that stops mid-charge would leave a plant charging at its maximum with no fuse cap of SpotNav's.
  So it is not mapped until a supervised test on a plant has shown what the inverter does after the watchdog and
  after a restart (`tools/foxess_watchdog_test.py`, which bounds that charge by a low max charge current first).
  Whether 41007/41008 and 46607/46608 are kept in flash is not stated anywhere: settings, 96 writes a day. Live writes
  wait for a person's hardware test on the plant's firmware (`hardware_test_first`).
* **EcoFlow PowerOcean** (`ECOFLOW_CONTROL`; **never run on a plant**), `ef_powerocean_tcpmodbus`, MaxGrmm/EF-PowerOcean-
  TcpModbus `56331de` (2.10.1, read 2026-11-24; `plans/research_integrations/ecoflow_control_2026-11-23.md`), local
  Modbus TCP, which EcoFlow support or the installer must first enable on the inverter. Unique id `<config entry
  id>_<key>` (`entity.py`). Switch `modbus_control` (`switch.py`: the integration's own flag, kept over a restart,
  that starts and stops its heartbeat), select `battery_mode` (`select.py`, options `ControlFeature` in
  `powerocean/models.py`: `automatic`, `hold_battery`, `charge_battery`, `discharge_battery`, `export_to_grid`,
  `import_from_grid`, `export_solar_first`; its state is the integration's selected mode, with the inverter's
  progress in the attribute `status`) and numbers `charge_battery_power` / `discharge_battery_power` (`number.py`,
  W, 0 up to the integration's ceiling, step 100: the power of those two modes, kept by the integration and sent only
  while that mode is selected). The select and the numbers are available only while the heartbeat holds control
  (`requires_modbus_control`). The integration writes control command 40534 (method 0 for Automatic, method 3
  "battery limits" for the rest) and the battery's signed setpoint 40571 (W, **positive charges**; Hold is 1 W, since
  0 is "no limit" to the inverter), and the heartbeat 40608 every 20 s. **The dead-man is the inverter's**: "the
  inverter runs on its app settings and merely stores the commands it is sent, until this register is written; each
  write buys it a minute of obeying them" (`powerocean/const.py`). So a command ends by itself within about a minute
  of Home Assistant stopping, but **not** while Home Assistant runs and SpotNav alone stalls: the integration keeps
  beating (`Expiry` renewer `integration`, which never lifts a cap). Turning the switch off writes nothing but stops
  the heartbeat, so a hand-back writes Automatic first (sent at once) and the switch off after; the person's hardware
  test writes the switch off alone (`Expiry.silence`) to time the inverter's own fallback. Hold: `hold_battery`
  (idle; at 100 % it curtails the panels, MaxGrmm #83); grid charge: the charge power, then `charge_battery`
  (a battery setpoint: sun on the DC side counts towards it); discharge to the house: `automatic` with the switch on;
  discharge to the grid: the discharge power, then `discharge_battery`. A plant is taken only in the app's
  self-consumption with Modbus Control off (`adapters.ECOFLOW`'s mode rules); the app is locked while the switch is
  on and for about a minute after. The inverter serves one Modbus client: `ecoflow_energy`'s local mode, evcc or an
  installer tool on the same inverter lock the integration out. Not mapped: `ecoflow_energy` (its local controls
  write the same registers with a client cap of 600 W by default) and every cloud route (no power setpoint on a
  PowerOcean). How a three-phase unit spreads a grid charge over its phases is not stated: `grid_charge_phases` 1.
* **Marstek** (Venus E v1/v2/v3, C, D, A), `marstek_modbus`, ViperRNMC/marstek_venus_modbus 2026.10.1 (`cb5a241`,
  `registers/e_v12.yaml`, `e_v3.yaml`, `a.yaml`, `d.yaml`, same keys in each) and 2026.11.1 (`f1db53b`, the rewrite:
  same keys, same `<config entry id>_<key>` unique ids, `entity.py` line 77, `venus/components.py` `Control`); read
  2026-11-24 (`plans/research_integrations/marstek_control_2026-11-23.md` has the background). **Never run on a
  device.** Switch `rs485_control_mode` (42000: on 0x55AA, off 0x55BB), select `force_mode` (42010: `standby`,
  `charge`, `discharge`), numbers `set_charge_power` (42020, W, 0-2500 on the Venus E, 0-1500 on the A, step 50) and
  `max_discharge_power` (44003, W, same range). All four are disabled by default. Registers 42000-42999 act only with
  RS485 control on (the integration's README); 44002/44003 do not need it. Every write is read back from the
  register (the integration polls the control block again a few seconds after a write, and every 10 s).
  **Limits first, and no dead-man.** No timeout or watchdog register is documented on any route but the local API's
  Passive countdown, which is unreliable in the field and has no read-back (research §3, §5.2): not used. So the hold
  is a limit that is safe if left standing (evcc's `holddischarge`): the maximum discharge power at 0, with the
  battery's own self-consumption (`user_work_mode` `anti_feed`) still storing a sun surplus. A grid charge is a force
  command that stays until changed (RS485 control on, the charge power, then the force mode `charge`), so it runs
  only under the HA-down cap (`gate.grid_charge_cap_kw`, all of it on the one phase the battery is on:
  `grid_charge_phases` 1); the battery's own `charge_to_soc` (42011) is never written, as nothing shows it ends a
  force charge. The hand-back is the force mode back to `standby`, then RS485 control off,
  which lands the battery back in its own work mode (evcc's templates note that turning it off "resets the device":
  to be seen in the hardware test); the discharge limit goes back to its value found. The charge power is never
  written back: it acts only in a force charge, a 42xxx write needs RS485 control on, and a change of action turns
  that off in the same pass; the next charge writes its own before the mode. A discharge to the house is
  the battery's own self-consumption, written as nothing; a discharge to the grid and the battery's maximum charge
  power (44002) are not mapped. A plant is taken only in `anti_feed` with RS485 control off (`adapters.MARSTEK`'s
  mode rules) and with the force mode at `standby` (`requires`: a force mode left standing would run the moment
  RS485 control goes on). evcc #36110 saw the maximum powers reset to 800 W after its own stop until toggled in the
  app: the limit's read-back and the hand-back's settle check show such a change. The local API (`marstek_local_api`)
  and Marstek's own `marstek` have none of these entities: the status names this integration as the one needed
  (`hacs_roles`). A battery run by Omnibattery is not this recipe's: Omnibattery is the Modbus client itself (one
  client at a time), so no `marstek_modbus` entity exists, and its presence marks the battery as run by someone else
  (`coexist.OUTSIDE_INTEGRATIONS`), never written over.
* **Solis** hybrids (`SOLIS_CONTROL`; **never run on a plant**), `solis_modbus`, Pho3niX90/solis_modbus v4.5.0
  (`f92ead8`, read 2026-11-25; `plans/research_integrations/solis_control_2026-11-22.md` Recipe A,
  `plans/review_solis_control_2026-11-25.md`). The inverter's remote-control force (register 43135) through the
  integration's services `solis_force_battery_charge` / `solis_force_battery_discharge` (`__init__.py`
  `_force_battery`: 43135, then the power, then the RC Timeout 43282, in one write batch, the order issue #352 needs)
  and `solis_stop_force_charge_discharge` (43135 = 0 alone). The select "RC Force Charge/Discharge" is never written:
  its own write re-sends the powers and the timeout from the integration's register cache (`companion_writes`), which
  holds whatever was last written by anyone. The services take no device (one inverter per Home Assistant: with more
  the call is refused, a write not taken). Read back: the force from the register's two switches "RC Force Battery
  Charge" / "... Discharge" (`<serial>_43135_1` / `_2`, updated on every register update) and the powers' sensors
  (43136/43129: a register reading, never the numbers' own optimistic echo), composed as `charge:<W>`,
  `discharge:<W>` or `stop` (`Role.compose`). Before a force, the power it runs at and the timeout are written as
  numbers with no force running and each read back by its register (the numbers show the value written at once, the
  register only once the write was executed, `modbus_controller.py` line 250), so a force whose call reaches the
  inverter only in part (a batch dropped after 43135 on a link outage) still runs at a power and a timeout SpotNav
  saw taken. **Hold**: force charge at 0 W (seen on a tester's S6-EH3P, 68 min at 0 W with the house at about
  1.8 kW; in daylight a sun surplus still charged it). **Grid charge**: force charge at the rate, within the BMS's
  present charge limit (`bms_charge_limit`: 10 A x 377 V held it at 3.7 kW in a cold garage); it needs "Allow Grid to
  Charge the Battery" (43110 bit 5) on, read and never written. **Discharge to the grid**: force discharge at the
  rate, a battery power (the house is covered first). What a force discharge at 0 W does is not known; never sent.
  **Discharge to the house**: the inverter's own Self-Use, written as nothing. **The hand-back** stops the force
  first, then sets both force powers to 0 (never back to the value found: someone's next force by hand through the
  select would run at it) and the timeout to its value found. **The dead-man** is the RC Timeout, sent at 5 minutes
  (the firmware's default) with each call and renewed every 2 minutes (`renewal_cap` 800 a day); stated by the
  integration (`__init__.py` line 108, "command self-reverts after this") and timed by the hardware test (from a
  renewal, so it also shows a renewal restarts it), never lifting a cap (`gate.SILENCE_FALLBACK_MAX_S`). A force that
  lapses by itself (a renewal lost) is handed back and started again (`command_role`). **Another system's force**
  (an outside EMS, SolisCloud): a plant is taken only in "Self-Use" with no force running and no V2 time-of-use
  period on (`adapters.SOLIS`'s mode rules); a force SpotNav did not write, seen at any time, is given up at once and
  nothing more is written to the force, the powers or the timeout (`command_role`, `mode_limits`), and it marks the
  battery as run by someone else (`outside_control.REASON_FOREIGN_COMMAND`). Below its Force Charge SOC (43018) the
  inverter charges from the grid by itself at its Force-charge Power Limitation (43027): declared as its own import
  (`AutonomousImport`, from the research, unverified), counted only while the SoC is near 43018
  (`near_reserve_only`). A power set from outside under SpotNav's own force (the same kind, another power) is left
  standing, nothing written, and stopped by SpotNav only for the fuse (`executor`'s `altered`); after a restart a
  hand-back waits for a reading of the force first (`_command_unread`). Remote Dispatch (44100) and SolisCloud's strategy are not read.
  **Phases** (owner decision 2026-11-26): a three-phase hybrid is counted as balanced, its grid charge and its
  discharge a third on each phase (`balanced_output`; a tester's S6-EH3P 15 kW put out the same on each phase within
  90 W while it covered a 4.7 kW house). Three-phase is read from the inverter itself, never from the model picked
  in the integration's setup (its `RHI-*` is declared three-phase for single-phase units too): the sensor "Inverter
  model definition" (`solis_modbus_inverter_model_definition`, register 35000's low byte as text,
  `helpers.py` `decode_inverter_model` lines 136-176 and `sensors/solis_derived_sensor.py` lines 205-209, v4.5.0
  `f92ead8`, read 2026-11-26), e.g. "S6 3-Phase HV Hybrid (12-20kW)(Protocol 3)". "3-Phase" in it: three phases;
  "1-Phase", a code the integration does not know ("Unknown Model"), or the sensor missing, disabled or unread: all
  on one phase (`grid_charge_phases` 1, `_solis_phases`). Nothing is known of flash wear
  on the RC registers: setpoints the maker states nothing about (500 a day; renewals bounded on their own).
* **Kostal Plenticore** (plus G1-G3, BI, MP G3; `KOSTAL_CONTROL`; **never run on a plant**), HACS
  `kostal_modbus_control`, mbuhansen/kostal_plenticore_battery_control 4.3.0 (`3bd84ac`, read 2026-11-25;
  `plans/research_integrations/kostal_control_2026-11-23.md`). Modbus TCP, which acts only with the inverter's
  installer setting "Battery management: External via protocol (Modbus TCP)" (register 1080 = 2; any other value and
  the inverter ignores every write without an error: the sensor `battery_mgmt_mode`, disabled by default, must read so
  before anything is written, `requires`). Core `kostal_plenticore` has no setpoint and is never written (its manual
  charge has no rate and no timeout). Unique ids `<config entry id>_<key>`. The integration's switches start and stop
  write loops of its own: **Charge Start** writes the battery's DC setpoint 1034 (1026, AC, on a BI) at minus **Set
  Charge Rate** (W, the integration's own value, restored by it over a restart, read each tick: what the switch's echo
  and the number's show is exactly what the loop sends; a rate at or above the number's maximum means "follow the
  maximum", which then moves with the battery's voltage, so a charge is always written 5 % or a step below it,
  whichever is more, `charge_power_bound_exclusive`, and a write read back following the maximum is not taken,
  `Role.follows_max`; the rate is written back last as found); **Block Discharge** holds the discharge limit 1040 at 0 (the integration's default;
  or the minimum SoC 1042 at the SoC, its option): the sun is still stored, and since the firmware of October 2026
  the inverter's own standby draw (about 170-260 W) may still come from the battery until the integration adds the
  SoC as the minimum. Each loop writes every half of the integration's timeout (`switch.py` `_loop_interval`), which
  must match the installer's timeout on the inverter (30 or 60 s seen, not readable). **The dead-man is the
  inverter's**: without a write for that long it falls back to its own control, so a command ends by itself within a
  minute of Home Assistant stopping (the switches are not restored over a restart), but **not** while Home Assistant
  runs and SpotNav alone stalls (`Expiry` renewer `integration`: never lifts a cap, and the integration's loops give
  SpotNav no write that stops them and leaves the command standing, so the hardware test cannot time it; a grid
  charge is always under the HA-down cap, `grid_charge_phases` 1). Switches are mutually exclusive in the
  integration (turning one on turns the others off): a change of action turns the old switch off first (a charge's
  stop by `peak_first`, a hold's release by the enter order), so SpotNav never sees its own switch turned off by the
  integration. Turning Charge Start off writes a "stop setpoint" from the live balance and then lets the inverter's
  timeout run out; the integration waits that timeout and 15 s before its next switch writes anything (a hold right
  after a charge starts up to 75 s late). Never written or relied on: Discharge Start (no sale), Block Charge,
  Inverter Control, EMS Grid Protection (left as found; it only ever lowers a charge), the SoC limits, the I/O
  outputs. The others' switches and an active Predbat (whose Charge Start runs Predbat's logic and whose reserve hold
  writes 1042) are read by `adapters.KOSTAL`'s mode rules. Nothing in Kostal's interface description says whether the
  battery registers are kept in flash; the integration and evcc say they are volatile and lapse with the timeout:
  setpoints at `SETPOINT_WRITES_UNSTATED`.
* **Fronius** (Gen24, Gen24 Plus, Tauro, Verto Plus with a BYD or Reserva battery), core `fronius` over Modbus TCP
  (SunSpec model 124; home-assistant/core `d33dfe3a`, 2026-11-23, in releases from 2026.11.0; `number.py`, `switch.py`,
  `coordinator.py`, `heartbeat.py`; `plans/research_integrations/fronius_control_2026-11-23.md`). **Never run on a
  device.** Numbers `battery_charge_power_limit` (InWRte) and `battery_discharge_power_limit` (OutWRte), % of the
  battery's WChaMax (`battery_power_limit_reference`, the adapter's rating), -100 to 100, step 1, each acting only
  while its switch (`battery_charge_power_limit_enabled` / `battery_discharge_power_limit_enabled`, one register,
  StorCtl_Mod) is on; a **negative** limit forces the other direction at least that fast. A hold is the discharge
  limit at 0 (a sun surplus is still stored), a discharge to the house the charge limit at 0 (it stores nothing), a
  grid charge the discharge limit at minus the rate's share of WChaMax, rounded down, 1 % at least (a minimum),
  with the charge limit at the same share as its cap (the research note: +p is what holds the total, so neither
  the sun nor the grid takes it above the rate). The rate is a share of the rating **as read** (`rating_read`): a learnt rating
  (WChaMax unread) is no base, the charge then does not start (`rated_unknown`). No discharge to the grid: a
  negative charge limit would be the first write of its kind and its sign is unconfirmed on a plant. The values
  are written before the switches, a switch turned off before the other one comes on where it matters
  (`peak_first` for the grid charge), and in a grid charge (both on, +p and -p) the limit that rises first
  (`rising_first`): core refuses a pair of enabled limits
  whose sum is below 0 (`_refused_battery_limits`), and a found negative value is handed back as 100, no limit
  (`Role.restored_below`), so a momentary pair never sums below 0. The core writes the setpoint, writes the
  enable bit again where it was already on (the device takes a changed limit only so) and reads the model back at
  once: read back by its `register`. Grid charging (`battery_grid_charging`, ChaGriSet) is read, never written: it
  is no part of the revert timer and stays when Home Assistant stops; off (or the web UI's "Battery charging from
  other sources" off, which cannot be read), no grid charge runs. The minimum reserve (MinRsvPct) is never written.
  **The revert timer** (InOutWRte_RvrtTms) drops both limits when no write comes for its period; core writes 3600 s
  and resends active limits every 15 minutes only with its opt-in option "Revert the power and battery limits if Home
  Assistant stops" on (0 otherwise): a dead-man the integration renews, not SpotNav (`renewer` `integration`), which
  never lifts a cap and keeps a stalled SpotNav's command alive while Home Assistant runs. SpotNav writes nothing to
  renew it (core's heartbeat does), hands back on every stop and unload, and a grid charge is always under the
  HA-down cap (`grid_charge_phases` 1: how a three-phase Gen24 spreads a forced charge is not stated). The option
  is in the config entry, not an entity: not read, only asked for in the docs. A limit switch on when SpotNav would
  start is someone else's (`requires`). Schedules, the Energy Cost Assistant and a grid operator's dynamic power
  reduction live in the web UI and cannot be read (`strategy_unread`). Nothing says whether model 124 is kept in
  flash: setpoints the maker states nothing about (500 a day).
* **Deye / Sunsynk / Sol-Ark** hybrids (`DEYE_CONTROL`; **never run on a device; limits only**), `solarman`
  (davidrapan/ha-solarman `84982c5`, the Deye profiles `deye_hybrid` and `deye_p3`) or kellerza's Sunsynk add-on over
  MQTT (`49efe3f`); read 2026-11-23 and 2026-11-25 (`plans/research_integrations/deye_sunsynk_control_2026-11-23.md`).
  Numbers "Battery Max Charging Current" / "Battery Max Discharging Current" (A, 0-240 on 1p, 0-350 on 3p LV; holding
  0xD2/0xD3 on 1p, 0x6C/0x6D on 3p), on kellerza "Battery Max Charge current" / "... Discharge current" (210/211 and
  108/109). `solarman` reads the register first and skips a write of the value it holds, writes it with FC16 and
  shows the value only once the logger acknowledged the write (`entity.py` `write`); the next poll shows the register.
  Nothing else is written with it (no companion registers, no cache). A **hold** is the discharge current at 0 (the
  battery covers nothing; a sun surplus is still stored: evcc's `holddischarge` on the 3p template), a **discharge for
  the house** the charge current at 0. Both only lower a limit, so if Home Assistant stops the battery only covers
  less: no fuse cap is needed and nothing expires (`NO_EXPIRY`). The hand-back writes both back as found; a limit
  someone else changed meanwhile (the vendor app, Predbat, evcc) is given up and not written back over them.
  **No grid charge and no sale.** There is no power setpoint and no dead-man on any route: a grid charge is the
  owner's six time-of-use programmes (each slot's SoC, power and charge source), which would have to be rewritten
  (evcc #34394, #36312), so it is not mapped. **Time of Use on** means the inverter runs its own programmes: the
  battery is someone else's and only watched (`adapters.DEYE`'s mode rule). The work mode, the grid charge switch and
  current, and the export and peak shaving settings are never written. **Writes**: every control is a settings
  register, and the community fears flash wear (kellerza #706; no limit is stated anywhere), so writes are only on a
  change of action, read back, and capped at `DEYE_SETTING_WRITES` a day, every write counted (`count_restores`: a
  hand-back always runs, but counts, and a cap it spent refuses the next start). Live writes wait for a person's hardware
  test on the plant's firmware (`hardware_test_first`).
* Enphase and brands without an adapter: **none**. (Enphase's controls in core are whole-mode switches or a reserve
  trick, see `battery_cycle_cost_and_modes_2026-11-13.md` part 3.)

Pure: no Home Assistant import. Nothing here writes; it says what would be written.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

from .adapters import (
    ECOFLOW, FERROAMP, FOXESS, GOODWE, HUAWEI, KOSTAL, MARSTEK, SIGENERGY, SOLAREDGE, SOLAX, SOLINTEG, SOLIS, TESLA,
    VICTRON,
    Adapter,
    Match, _goodwe, _key, _tesla,
)
from .adapters import FRONIUS, _fronius
from .adapters import DEYE, _any_key
from .adapters import SUNGROW, _package
from .plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
)

#: The value a role had before SpotNav first wrote it.
SNAPSHOT: Final = "<snapshot>"
#: How a number that follows its own maximum is read (`Role.follows_max`): a text, never a number, so a value written
#: to it never reads back as taken while the entity follows its maximum instead.
FOLLOWS_MAX: Final = "follows_max"
#: Values a recipe cannot fix on its own, filled in by `BrandControl.wanted` from the plant's readings when the
#: action is entered: `floor(SoC now)` (Tesla's reserve under a hold), the depth of discharge that makes the cut-off
#: the SoC now (GoodWe's hold), and a share of the battery's rated power for a power in kW (`pct_of_rated`). Left as
#: they are when the reading is missing: the executor then refuses to start (`unresolved`).
SOC_FLOOR: Final = "<soc_floor>"
SOC_DEPTH: Final = "<soc_depth>"
_PCT_PREFIX: Final = "<pct_of_rated:"
#: A charge ceiling one block ahead of the SoC now (`soc_ceiling`, `BrandControl.charge_block_s`).
_CEILING_PREFIX: Final = "<soc_ceiling:"

#: How long a brand's own watchdog runs (s) and when SpotNav renews it: a command SpotNav stops renewing (HA died)
#: falls back by itself within this.
WATCHDOG_S: Final = 1800
WATCHDOG_RENEW_S: Final = 900

RoleMethod = Literal["number", "select", "switch", "button", "service", "sensor"]
#: What a role is, for how often it may be written in a day (`BrandControl.write_caps`): a `setting` (a limit, a mode,
#: a switch: possibly stored in flash) or a `setpoint` made for continuous outside control.
WriteClass = Literal["setting", "setpoint"]
#: Writes a day per class, outside restores and watchdog renewals. A setting, and anything of a brand or role not
#: checked: 96. A setpoint whose maker says it is volatile or made for continuous control: about 2000 (still a
#: runaway guard: reaching it hands back). A setpoint the maker's protocol says nothing about: 500.
SETTING_WRITES: Final = 96
SETPOINT_WRITES_UNSTATED: Final = 500
#: How long a write may take to read back (s) where the plant's link stops answering now and then (Solinteg's dongle:
#: 4-8 s about once a minute, about 70 s seen once), instead of the ledger's `READBACK_DEADLINE_S`.
READBACK_STALL_S: Final = 180.0
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
class Answer:
    """Where a device answers each request in the entity's attributes (`Role.answer`): the attribute that names the
    request's transaction, the one that holds the device's answer to it, and the answers that mean it was taken."""

    transaction: str
    status: str
    taken: frozenset[str]


#: How a command ends by itself when nothing renews it: never (`none`), the device's own dead-man on the last write
#: (`device_deadman`), a lease the command itself carries (`device_lease`: a duration, an autorepeat), a SoC bound the
#: command stops at (`soc_bound`: a ceiling one block ahead), or the end of a clock window (`clock_window`).
ExpiryKind = Literal["none", "device_deadman", "device_lease", "soc_bound", "clock_window"]
#: Who keeps the command alive: SpotNav's own renewals, an integration's own loop (which a stalled SpotNav does not
#: stop: never counted as an expiry against SpotNav), or nobody.
Renewer = Literal["spotnav", "integration", "nobody"]
#: The recipe's own claim (`stated`, never enough to lift a fuse cap) or a field test's (`measured`, per firmware).
Evidence = Literal["stated", "measured"]
#: How a write to a role is read back: the device's own register, Home Assistant's echo of the value sent, a value
#: the device confirms after a delay, the device's answer in the attributes (`Role.answer`), or not at all.
ReadBack = Literal["register", "echo", "verified", "answer", "none"]

_RENEWED_KINDS: Final = frozenset({"device_deadman", "device_lease", "soc_bound"})


@dataclass(frozen=True, slots=True)
class Expiry:
    """How a recipe's command ends by itself when Home Assistant stops writing, and what renews it meanwhile. Data
    only; the gate decides what it is worth (`gate.expiry_lifts`): a `stated` expiry never lifts a fuse cap."""

    kind: ExpiryKind = "none"
    renewer: Renewer = "nobody"
    #: The dead-man's or the lease's length (s); for `soc_bound` the block; for `clock_window` the slot's length.
    window_s: float | None = None
    #: The roles SpotNav rewrites to keep the command alive (only with `renewer` `spotnav`); for `soc_bound` the
    #: ceiling's role.
    renewed_roles: tuple[str, ...] = ()
    #: How often they are rewritten (s); `None`: `WATCHDOG_RENEW_S`.
    renew_s: int | None = None
    evidence: Evidence = "stated"
    #: The `gate.firmware_key` values a field test saw the fallback act on (only with `evidence` `measured`).
    measured_on: frozenset[str] = frozenset()
    #: For a phase spec's expiry (`PhaseControl.expiry`): the phases (`L1`..`L3`) that field test saw it act on;
    #: only those phases' caps are lifted by it (owner decision 2026-11-23). Required there with `measured`.
    measured_phases: frozenset[str] = frozenset()
    #: For `renewer` `integration`: which integration renews it (diagnostics text).
    integration: str = ""
    note: str = ""
    #: What a renewed role reads once the device itself fell back, where the maker documents it (`(role, value)`):
    #: only such a reading in the verification's silence is the device's own (`expired`); any other change of a
    #: renewed role there is another writer. Empty: the device leaves them as written (Victron's setpoints).
    fallback_values: tuple[tuple[str, str], ...] = ()
    #: For a dead-man the integration renews: the writes that stop its renewal and leave the command standing
    #: (`(role, value)`; EcoFlow: Modbus control off stops the integration's heartbeat and writes nothing else), so
    #: the device's own fallback can be timed. Only a person's hardware test writes them (`gate.deadman_testable`,
    #: the verification's mode `expiry`). Empty: SpotNav cannot make the silence itself.
    silence: tuple[tuple[str, Native], ...] = ()

    def __post_init__(self) -> None:
        if self.silence and (self.kind != "device_deadman" or self.renewer != "integration"):
            raise ValueError("a silence is written only for a dead-man an integration renews")
        renews = self.renewer == "spotnav" and self.kind in _RENEWED_KINDS
        if bool(self.renewed_roles) != renews:
            raise ValueError("renewed_roles are set exactly for an expiry SpotNav renews")
        if self.kind == "device_deadman" and self.renewer == "nobody":
            raise ValueError("a dead-man needs something that renews it")
        if self.kind != "none" and (
            self.window_s is None or isinstance(self.window_s, bool) or not math.isfinite(self.window_s)
            or self.window_s <= 0
        ):
            raise ValueError("window_s must be a positive finite number")
        if self.renew_s is not None and (
            self.renew_s <= 0 or (self.window_s is not None and self.renew_s >= self.window_s)
        ):
            raise ValueError("renew_s must be positive and shorter than window_s")
        if self.evidence == "measured" and not self.measured_on:
            raise ValueError("a measured expiry names the firmware it was measured on")
        if (
            renews and self.kind != "soc_bound" and self.renew_s is None and self.window_s is not None
            and self.window_s <= WATCHDOG_RENEW_S
        ):
            raise ValueError("a window shorter than the shared renewal needs its own renew_s")
        if any(role not in self.renewed_roles for role, _value in self.fallback_values):
            raise ValueError("a documented fallback value is for a renewed role")
        if not self.measured_phases <= {"L1", "L2", "L3"} or (self.measured_phases and self.evidence != "measured"):
            raise ValueError("measured_phases names L1, L2 or L3, and only for a measured expiry")

    @property
    def expiry_id(self) -> str:
        """What a verification row names the expiry it measured by (`<kind>:<window>:<roles>`): a measured phase
        expiry never counts for a balanced one."""
        window = "-" if self.window_s is None else f"{self.window_s:g}"
        return f"{self.kind}:{window}:{','.join(self.renewed_roles)}"


NO_EXPIRY: Final = Expiry()


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
    #: For a service role: the call's field that names the battery's device (Huawei `device_id`, Ferroamp `target`),
    #: and whether a call with a power is a lease: it carries the recipe's expiry window (`Expiry.window_s`) as its
    #: `duration` in minutes (`service_call`).
    device_field: str = "device_id"
    lease: bool = False
    #: For a service role: the call's fields for the power (W) and a lease's minutes, where the integration names them
    #: otherwise (Solis's `power_watts`, `duration_minutes`); an empty `device_field` sends no device at all (the
    #: integration finds its one inverter itself).
    power_field: str = "power"
    duration_field: str = "duration"
    #: The role is read back from more than its own entity (`parts`, found as `match` is): `compose(own state, the
    #: parts' states)` is the state compared, `None` when it cannot be told (Solis: the force's two switches and the
    #: force powers' register readings make `charge:<W>`, `discharge:<W>` or `stop`).
    parts: tuple[Match, ...] = ()
    compose: Callable[[str | None, tuple[str | None, ...]], str | None] | None = None
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
    #: A role whose state shows the last request, not that the device took it (Ferroamp's last command): the
    #: device's answer in the attributes, without which the state is no reading (`answered`).
    answer: Answer | None = None
    #: How a write to it is read back (`ReadBack`): the device's `register` (the default), Home Assistant's `echo` of
    #: the value sent (the battery's power then judges the effect), the device's `answer` (exactly where `answer` is
    #: set), or `none` (a button).
    readback: ReadBack = "register"
    #: How long a write may take to read back (s) before it counts as not taken; `None`: the ledger's own
    #: (`ledger.READBACK_DEADLINE_S`). Longer only for a link known to stall (`READBACK_STALL_S`) or a cloud, and only
    #: for a write that starts or changes a command: a restore and a peak cover keep the ledger's own, so a dropped
    #: one is sent again soon.
    readback_s: float | None = None
    #: How long after a hand-back a write to it is watched (s): where the read-back is only Home Assistant's own echo
    #: a device or a cloud that refused the restore shows SpotNav's value again on a later poll, and the restore is
    #: then written again (`executor.SETTLE_ATTEMPTS`). 0: not watched.
    settle_s: float = 0.0
    #: How near a read must be to a value to count as it (in the entity's unit), where the entity's own step is too
    #: coarse for that: a value one unit off means something else (Victron's -1, no limit, beside 0). `None`: the
    #: entity's step, else `step`.
    tolerance: float | None = None
    #: The entity may not exist on a plant of this brand (Tesla's grid-charging switch needs solar): when it is not
    #: found the role is left out of every state and nothing is missing.
    optional: bool = False
    #: `(below, value)`: a number found below `below` is restored as `value` instead (in the entity's unit); `None`:
    #: as found. Victron's grid setpoint far below 0 is a sale running when the snapshot was taken (SpotNav's own left
    #: by a lost state, or another tool's), never a value to put back for good: the GX would then sell the battery
    #: until someone noticed. The executor logs each such replacement (`restore_replaced`).
    restored_below: tuple[float, float] | None = None
    #: The attribute that is true while the number follows its own maximum instead of the value it shows (Kostal's
    #: Set Charge Rate: a value at or above the maximum, which moves with the battery's voltage). The role then reads
    #: `FOLLOWS_MAX` (`answered`): a write that latched it is not taken (no number read back), and the restore of a
    #: value found so writes the entity's maximum again (`control.resolve_follows_max`). `None`: no such attribute.
    follows_max: str | None = None
    #: The value the maker documents as "no limit" (Victron's -1 on its feed-in limit), in the entity's unit; `None`:
    #: none. Found so, it is handed back exactly (never moved into a range), a read below it is no value at all
    #: (`executor._restore_value`), and the diagnostics name it (`export_table`).
    no_limit: Native | None = None

    def __post_init__(self) -> None:
        if (self.readback == "answer") != (self.answer is not None):
            raise ValueError(f"{self.name}: read back by the device's answer exactly where the role has one")


@dataclass(frozen=True, slots=True)
class Want:
    """The state a role must be in for an action: a native value, or `SNAPSHOT`."""

    role: str
    value: Native


@dataclass(frozen=True, slots=True)
class HoldSurplus:
    """A hold whose charge limit follows the sun's measured surplus (`executor`'s surplus follow), so the sun is
    stored while the hold still takes nothing from the grid; the hold's own build writes the limit at 0. Data only.

    The new limit is the surplus now (the house's net load, `executor.house_net_kw`, below zero), in `step_kw`, and
    0 under `deadband_kw`. It is written when it differs from the limit standing by at least `deadband_kw` or is 0,
    a raise no sooner than `raise_gap_s` after the last one; a fall at once, so the grid takes at most about the
    deadband while the sun fades."""

    role: str
    deadband_kw: float = 0.4
    step_kw: float = 0.1
    raise_gap_s: float = 60.0


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
    #: The charge limit follows a sun surplus (`HoldSurplus`); the hold then stores the sun and is no longer idle.
    surplus: HoldSurplus | None = None


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
    #: Other roles only the phased control writes (in `follow`: a scheduling mode), so a plant without them shows no
    #: missing control for the balanced one.
    modes: tuple[Role, ...] = ()
    #: How the charge per phase ends by itself when nothing renews it, and the roles SpotNav renews to keep it alive
    #: while the phased control runs (`BrandControl.watchdog_roles`, after the control's own) and how often (`None`:
    #: `WATCHDOG_RENEW_S`). Read by the gate (`gate.grid_charge_phase_caps_kw`): only a dead-man **measured** on a
    #: plant may let a phase's HA-down cap be the fuse itself (a maker's page alone is not enough).
    expiry: Expiry = NO_EXPIRY
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
    #: The three targets exist only on a three-phase plant (an entity per phase the plant has): finding them is
    #: what shows the plant's phases, where the brand has no other reading of them.
    three_phase_by_targets: bool = False
    #: The largest size a `grid` target is written at (kW): the entity's own range, which a write beyond would be
    #: refused by. A target cut to it leaves the battery less than its charge, never more.
    limit_kw: float | None = None

    @property
    def watchdog_roles(self) -> tuple[str, ...]:
        """Roles renewed to keep the charge per phase alive (`expiry`)."""
        return self.expiry.renewed_roles

    @property
    def watchdog_renew_s(self) -> int | None:
        """How often the `watchdog_roles` are renewed (s); `None`: `WATCHDOG_RENEW_S`."""
        return self.expiry.renew_s

    def __post_init__(self) -> None:
        if self.expiry.evidence == "measured" and not self.expiry.measured_phases:
            raise ValueError("a phase expiry measured in the recipe names the phases it was measured on")

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
        """Every role the phased control adds: the targets, the others kept at 0, what `follow` writes, its modes, what
        must read so, and for a `grid` target the sensors at and behind its point."""
        return (
            *self.zero, *self.roles, *self.targets, *self.modes, *(role for role, _states, _why in self.requires),
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
class ImportLimitPhases:
    """A plant whose own grid import limit (`BrandControl.import_limit`) holds **each phase** at a third of it, not
    only the total: what lets a grid charge run without the HA-down cap (`gate.grid_charge_cap_kw`,
    `gate.import_limit_phase_holds`). Data only.

    It holds only where everything here reads so, checked at every start and every look: the switch that makes the
    plant share its power per phase reads `on`, the plant's grid sensor (which the limit regulates on) reads
    `sensor_online`, and the plant's Output Type reads `output_type` (`BrandControl.output_type`, read, never assumed).
    `field_tested` is the recorded result of the owner's on-site test (`tools/sigen_import_limit_test.py`): a
    single-phase load held at a third of the limit at full charge, **while the total stayed under the limit** (a
    plant that only shares out a cut once the total binds lets one phase reach `L / 3 + 2 (p + cars) / 3` just below
    that point, which is the total-only rule, `gate.import_limit_max_kw`) and with Home Assistant's writes stopped.
    Until it is recorded the rule never acts."""

    #: The switch that makes the plant share its power per phase (read, never written); it must read `on`.
    switch: Match
    #: The plant's grid sensor status and the state that means it is connected.
    grid_sensor: Match
    sensor_online: str
    #: The Output Type (lower-cased) the switch acts with: Sigenergy's 40030 is valid only for L1/L2/L3/N.
    output_type: str
    #: The owner's on-site test passed (see the class). Off until it has.
    field_tested: bool = False


@dataclass(frozen=True, slots=True)
class AutonomousImport:
    """What a plant may import from the grid by itself, uncommanded, while SpotNav runs it (a top-up below its
    reserve, a schedule of its own, a set grid input): counted with every HA-down cap (`gate`'s `own_import_a`,
    whatever the trigger: the SoC drifts while Home Assistant is down) and in the executor's live room while the
    trigger may fire. Data only; Solis declares one (its own force charge below its Force Charge SOC)."""

    #: A fixed figure (kW), or a number or sensor to read it from (`entity`, times `scale` to kW); read first, the
    #: figure where it reads nothing, and no grid charge at all where neither is known.
    kw: float | None = None
    entity: Match | None = None
    scale: float = 1.0
    #: The phases it is spread over; `None`: the recipe's own (`grid_charge_phases`, or the Output Type).
    phases: int | None = None
    trigger: Literal["always", "below_reserve", "own_schedule"] = "always"
    #: For `below_reserve`: the reserve (%) it tops up to, where the plant shows it (else the facts' own reserve).
    reserve: Match | None = None
    why: str = ""
    #: For `below_reserve`: counted in the HA-down caps too only while the SoC is within
    #: `outside_control.RESERVE_MARGIN_PCT` of the reserve (or not read): a top-up that can only start that low, far
    #: under the levels SpotNav charges at (Solis's own force charge below its Force Charge SOC). `False`: counted in
    #: them whatever the SoC (the SoC drifts while Home Assistant is down).
    near_reserve_only: bool = False


@dataclass(frozen=True, slots=True)
class ImportLimitTrust:
    """Where a plant's own grid import limit (`BrandControl.import_limit`) was shown to act, so that either import-limit
    rule may stand in for the HA-down cap (`control.BatteryControl._import_limit_refusal`: `not_trusted` elsewhere).
    Data only."""

    #: The plan actions in whose commanded state the limit was shown to act.
    acts_in: frozenset[str]
    #: The plant firmwares (as read, the firmware part of `gate.firmware_key`) it was shown to act on; `None`: any.
    firmware: frozenset[str] | None = None
    #: Non-empty: never trusted, the offer hidden (the reason, e.g. a known bug of the maker's).
    untrusted: str = ""


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
    #: The plant's own strategy cannot be read (no mode rule shows an optimiser, peak shaving or a grid service), so
    #: it always reads free: nothing is written until the person acknowledged that SpotNav's commands override
    #: whatever strategy runs (the same acknowledgement as `takeover`, `ExecInputs.takeover`); the hand-back gives
    #: the plant back to that strategy.
    strategy_unread: bool = False
    #: How a command ends by itself when nothing renews it, and the roles SpotNav renews meanwhile (`Expiry`;
    #: `watchdog_roles`, `watchdog_renew_s`). The phase spec's own is the phase control's (`PhaseControl`), never
    #: this one: a phased control's balanced commands keep the brand's.
    expiry: Expiry = NO_EXPIRY
    #: Roles that must read a value before anything is written (`(role, allowed states, reason)`).
    requires: tuple[tuple[str, frozenset[str], str], ...] = ()
    #: Extra requirements for one action (`(action, role, forbidden states, reason)`); a forbidden state that is a
    #: number matches a read of the same number ("0" matches "0.0"), `forbidden_read`.
    action_requires: tuple[tuple[str, str, frozenset[str], str], ...] = ()
    #: Per action that adds import, the roles that let the battery cover a peak again, written first (`executor`'s
    #: peak cover under a hold, and the first writes of every hand-back of that action).
    peak_first: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: `battery` (the plan's actions) or `export` (the export limit, `ACTION_CURTAIL_EXPORT`): an export control
    #: needs no battery reading or fuse headroom, since a lower export limit at the grid point never adds import
    #: (`limit_point`).
    kind: Literal["battery", "export"] = "battery"
    #: Where an export control's limit acts: `grid` (what leaves the site at its grid connection: the house is still
    #: fed by the sun) or `inverter_ac` (the inverter's whole AC output: the house would then be fed from the grid).
    #: An export control must say `grid` (`__post_init__`): a cap on the inverter's AC output (Fronius's and Kostal's
    #: power limits, Solis's output clamp, SolarEdge's active power limit, Solinteg's 50208) is never one, and that is
    #: why it may skip the fuse headroom. `None` for a battery control.
    limit_point: Literal["grid", "inverter_ac"] | None = None
    #: For an export control: a source of the maker's or the integration's states that 0 means no export (not "off"
    #: or "unlimited"). Only for the gate and the report (`control`'s export check), never for what is written.
    zero_stated: bool = False
    #: How the plant spreads a grid charge over the phases: as many phases, evenly (the HA-down grid charge cap,
    #: `gate.grid_charge_cap_kw`); 1 (all on one phase) where the brand does not say. `output_type` (an enum sensor,
    #: read by `OUTPUT_TYPE_PHASES`) overrides it when the plant shows it.
    grid_charge_phases: int = 1
    output_type: Match | None = None
    #: How the `output_type` entity's state reads as phases, where it is not Sigenergy's enum (`OUTPUT_TYPE_PHASES`);
    #: `None` from it (a state it does not know) is the recipe's `grid_charge_phases` (`plant_phases`).
    output_phases: Callable[[str], int | None] | None = None
    #: A three-phase plant of this brand puts out the same on each phase (it balances the sum at the meter, not
    #: each phase's own load): what its discharge covers comes back a third on each phase when it stops
    #: (`executor.cover_a`). `False` (a SigenStor covers each phase on its own): all of it may be on one phase.
    balanced_output: bool = False
    #: The plant's own grid import limit (kW): while it reads at most the fuse, and the person's on-site test showed
    #: it acts (`battery_control_import_limit_acts`), it bounds a grid charge with Home Assistant down.
    import_limit: Match | None = None
    #: Where that limit is held per phase (`ImportLimitPhases`): with the owner's test recorded, the HA-down cap is
    #: lifted while it reads at most what keeps every phase under the fuse (`gate.import_limit_phase_holds`).
    import_limit_phases: ImportLimitPhases | None = None
    #: Where that limit was shown to act (`ImportLimitTrust`); set wherever `import_limit` is.
    import_limit_trust: ImportLimitTrust | None = None
    #: What the plant may import by itself, uncommanded (`AutonomousImport`); `None`: nothing (every recipe today:
    #: Sigenergy in Maximum Self Consumption under SpotNav has none, its AI mode's own charge is outside control).
    autonomous_import: AutonomousImport | None = None
    note: str = ""
    #: Ways to hold, tried in order by the verification (`HoldVariant`); the first is in `states` and `peak_first`
    #: until another is chosen (`with_hold`). Empty: the brand has one hold.
    hold_variants: tuple[HoldVariant, ...] = ()
    #: The hold variant this control runs (`None`: the brand has one hold).
    variant: str | None = None
    #: Limits that act in the plant's modes (`executor`'s change of action): on a change, one whose value falls is
    #: written before every other role and one whose value rises after them all, so a new mode never runs on the old
    #: action's looser limit, nor the old mode on the new one's (Sigenergy: Command Charging (PV First) at the charge
    #: limit a grid charge left at the rating would charge from the grid at full power until the limit follows).
    mode_limits: tuple[str, ...] = ()
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
    #: A slow control (a cloud: Tesla): planned actions only. No hold for a car, no peak cover, no change of action
    #: sooner than `min_change_s`, the read-back waits `readback_s` and a hand-back waits `rearm_s` before the next
    #: action (`executor/`, `control.py`). `None`: the executor's own figure.
    slow: bool = False
    min_change_s: float = 0.0
    rearm_s: float | None = None
    #: How long a person's hardware test of the recipe may run in all (s; `hardware_test.HardwareTest._max_test`),
    #: `None`: the test's own ten minutes. A slow recipe's modes each wait for its re-arm and its cloud read-backs
    #: (`verify.quick_for`), so its test takes about an hour.
    test_total_s: float | None = None
    #: The plant charges at its own rate and the recipe cannot set it (Tesla): the gate and the executor use that rate
    #: (`fixed_rate_kw`) and refuse a grid charge that does not fit under the fuse cap instead of lowering it.
    fixed_charge: bool = False
    #: Its rate: kW per `fixed_unit_kwh` of the battery's size (at least one unit); the plant's own rating counts when
    #: it is larger.
    fixed_charge_kw: float = 0.0
    fixed_unit_kwh: float = 13.5
    #: Roles that exist only in a custom integration: not found, the status says `needs_hacs_integration` (GoodWe's
    #: eco power and SoC numbers are the HACS fork's, not core's).
    hacs_roles: tuple[str, ...] = ()
    #: The custom integration those roles come from (`owner/repository`), named in the status' need.
    hacs_repo: str = ""
    #: Percentage points a floor set to the SoC now (`SOC_FLOOR`, `SOC_DEPTH`) is kept below it: a SoC read late (a
    #: cloud's, up to a minute old, while the battery discharges) must not put the floor above the real level, where
    #: the plant charges from the grid to reach it.
    soc_floor_margin_pct: float = 0.0
    #: The hold is a floor at the SoC now (Tesla's reserve, GoodWe's depth of discharge), which the plant may reach
    #: from the grid: a hold whose battery charges from the grid (the lesser of its charge and the site's import)
    #: did not take, and is handed back as an idle hold that charges is (`executor.HOLD_CHARGE_S`). The sun may
    #: still be stored.
    hold_grid_guard: bool = False
    #: Optional roles whose absence lets a hold itself charge from the grid at the plant's fixed rate (Tesla without
    #: the grid-charging switch: a reserve above the SoC then charges from the grid, whatever the margin): such a
    #: hold runs only while that rate fits under the fuse cap, as a fixed grid charge must (`gate.restricted`,
    #: `executor`'s start rule).
    hold_charges_without: tuple[str, ...] = ()
    #: An action's own expiry where it differs from the control's (`expiry`): a grid charge whose power the plant
    #: sets (`fixed_charge`) and that stops by a SoC ceiling (`soc_ceiling`) is a `soc_bound` expiry, commanded in
    #: blocks of its window (`charge_block_s`).
    expiry_by_action: Mapping[str, Expiry] = field(default_factory=dict)
    #: How often a restore given up is tried again while the control is blocked (s), where each write costs (a
    #: cloud's billed commands); `None`: `executor.BLOCKED_RESTORE_S`.
    restore_retry_s: float | None = None
    #: The recipe has been tested on a real plant of the brand (the owner's on-site tests): control on it leaves
    #: shadow only by default (`gate.Options.from_config`). An untested recipe stays shadow only unless the site is a
    #: beta tester (`beta.learning_gate_open`). Its verification routine and every fuse rule apply either way.
    field_tested: bool = False
    #: Until the recipe is `field_tested`, nothing is written by the routine or by control (shadow only, whatever the
    #: site's option says, beta site or not) until a person's hardware test (the card's Support box,
    #: `hardware_test.py`) passed on the firmware the plant runs (`gate.firmware_key`, `gate.hardware_test_passed`): a
    #: recipe never run on a device, whose first writes should be supervised. The checklist says so
    #: (`hardware_test_first`). The test itself and every hand-back still run. `False`: the routine alone.
    hardware_test_first: bool = False
    #: The recipe's revision: bumped with any change to what the recipe writes or in which order. From 2 it is part
    #: of the report key (`BatteryControl.firmware_key`), so the routine checks a changed recipe on each plant again
    #: before control relies on it, as after a firmware update; `blocked` can hold one revision back.
    recipe_rev: int = 1
    #: The role whose entity's own range bounds a grid charge (its `max`, over the role's scale): for a recipe whose
    #: models take less than the recipe allows (Marstek: 2500 W on a Venus E, 1500 W on a Venus A). `None` for every
    #: other recipe, whose entities' ranges are never read (Home Assistant's default 0..100 would read as 0.1 kW).
    charge_power_bound: str | None = None
    #: That entity's maximum itself means something else (Kostal's Set Charge Rate: "follow the battery's maximum",
    #: which then moves with its voltage and would read as someone else's change), so a charge is bounded one of the
    #: role's steps below it. `False` for every other recipe.
    charge_power_bound_exclusive: bool = False
    #: The battery's BMS bounds a grid charge live: its present charge-current limit times its voltage
    #: (`facts.BatteryFacts.bms_charge_kw`), read every look, for the check's charge step, a live grid charge, a test
    #: action and the plan's cap alike; it only ever lowers a cap (Solis: a cold Dyness at 10 A x 377 V takes 3.7 kW
    #: whatever the force asks, and the monitor would read the rest as a charge not taken). `False` for every other
    #: recipe.
    bms_charge_limit: bool = False
    #: The role that holds the plant's whole command (Solis's RC force): a read of it that is neither SpotNav's value
    #: nor its documented fallback nor the value found, of another kind (the text before `:`), is another system's
    #: command, given up at once (no dwell), and from then on nothing is written to it or to `mode_limits`, not even
    #: by the hand-back (`executor`'s `left_alone`). Its documented fallback (`Expiry.fallback_values`) read while a
    #: command runs is the command lapsing by itself: handed back and started again (`lapsed`), blocked only after
    #: `LAPSES_A_DAY`. `None`: every role is the ledger's alone, as before.
    command_role: str | None = None
    #: The renewals of the expiry a day (`Expiry.renewed_roles`; each a write to the plant that the day's write caps
    #: never count): reaching it hands the command back and starts none again that day (`renewal_cap`). `None`: not
    #: bounded (the recipes whose renewal is a few writes an hour).
    renewal_cap: int | None = None
    #: The hardware test's dead-man step must have passed (timed), not merely been tried, for the test to pass: a recipe
    #: whose every action is a command that stands until it expires (`gate.hardware_test_passed`).
    deadman_must_pass: bool = False
    #: The recipe's shares of the rated power (`pct_of_rated`) are of the rating as the plant reads it (Fronius's
    #: WChaMax, the base its limits are a percentage of): a rating learnt where the plant reads none
    #: (`learned.with_learned`) is no base for them, so they stay unresolved (`rated_unknown`). `False`: any rating.
    rating_read: bool = False
    #: A hand-back writes a role back only over SpotNav's own value: one that reads neither SpotNav's value nor the
    #: one found (the vendor app or another controller set it meanwhile, also while Home Assistant was down) is left
    #: alone (`left_alone`), and after a restart a role with no reading yet is not written blind until it reads or
    #: `executor.RESTORE_READ_WAIT_S` passed (never for the fuse). For settings whose found value is the owner's (Deye's
    #: current limits). `False`: as before (a restart restores every written role).
    restore_own_only: bool = False
    #: Number roles of which any that rises from what it reads to its target is written ahead of the brand's order
    #: (after a change's stop): limits whose enabled pair must never sum below 0 (Fronius: the charge cap at +p beside
    #: the discharge limit at -p), so a change of rate never passes through a pair core refuses. `()`: the order.
    rising_first: tuple[str, ...] = ()
    #: Restores count against the day's write cap like every other write (`executor._progress`): never held back by
    #: it (a hand-back, a fuse's too, always runs), but a cap spent by them refuses the next start. For settings whose
    #: every write is a register write nobody states the wear of (Deye's current limits). `False`: not counted.
    count_restores: bool = False

    def __post_init__(self) -> None:
        if self.command_role is not None and self.command_role not in {role.name for role in self.roles}:
            raise ValueError(f"{self.brand}: the command role is a role of the control's own")
        if self.charge_power_bound is not None and self.charge_power_bound not in {role.name for role in self.roles}:
            raise ValueError(f"{self.brand}: the charge power bound names a role of the control's own")
        if self.import_limit is not None and self.import_limit_trust is None:
            raise ValueError(f"{self.brand}: an import limit needs to say where it was shown to act")
        names = {role.name for role in self.roles}
        if any(role not in names for role, _value in self.expiry.silence):
            raise ValueError(f"{self.brand}: a silence writes the control's own roles")
        if self.kind == "export" and self.limit_point != "grid":
            raise ValueError(f"{self.brand}: an export control's limit acts at the grid point, never on the AC output")

    @property
    def watchdog_roles(self) -> tuple[str, ...]:
        """The roles renewed to keep a watchdog alive: the expiry's, and on a phased control (`with_phases`) the phase
        spec's after them; empty when the brand has none."""
        spec = self.phase_control
        extra = spec.expiry.renewed_roles if self.phased and spec is not None else ()
        return (*self.expiry.renewed_roles, *extra)

    @property
    def watchdog_renew_s(self) -> int | None:
        """How often the `watchdog_roles` are renewed (s): the phase spec's own on a phased control where it states
        one, else the expiry's; `None`: `WATCHDOG_RENEW_S`."""
        spec = self.phase_control
        if self.phased and spec is not None and spec.expiry.renew_s is not None:
            return spec.expiry.renew_s
        return self.expiry.renew_s

    @property
    def local_readback(self) -> bool:
        """Whether the effect can only be judged by the battery's power: a role read back by Home Assistant's echo,
        by the device's answer to a request, or not at all (`Role.readback`)."""
        return any(role.readback in ("echo", "none", "answer") for role in self.roles)

    @property
    def readback_s(self) -> float | None:
        """The longest read-back wait of the control's roles (`Role.readback_s`); `None`: the ledger's own for all."""
        waits = [role.readback_s for role in self.roles if role.readback_s is not None]
        return max(waits) if waits else None

    @property
    def settle_s(self) -> float:
        """The longest a hand-back's writes are watched (`Role.settle_s`); 0: not watched."""
        return max((role.settle_s for role in self.roles), default=0.0)

    @property
    def charge_block_s(self) -> float | None:
        """A grid charge whose power the plant sets (`fixed_charge`) and that stops by a SoC ceiling (`soc_ceiling`)
        is commanded in blocks of this long (s; the grid charge's `soc_bound` expiry): the ceiling is what the plant
        reaches in one block at the recipe's rate (an upper figure, so it is reached no sooner), renewed every block
        from the SoC then. Without a dead-man a Home Assistant (or a cloud) that stops leaves at most one block's
        charge from the grid, not a full battery. `None`: no such block."""
        expiry = self.expiry_by_action.get(ACTION_GRID_CHARGE)
        return expiry.window_s if expiry is not None and expiry.kind == "soc_bound" else None

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
        """Whether this control's hold leaves the battery idle (`HoldVariant.idle`); a hold that follows a sun
        surplus (`hold_surplus`) is not."""
        return any(
            variant.idle and variant.surplus is None for variant in self.hold_variants if variant.name == self.variant
        )

    @property
    def hold_surplus(self) -> HoldSurplus | None:
        """How this control's hold follows a sun surplus (`HoldVariant.surplus`); `None`: its hold writes are fixed."""
        return next((variant.surplus for variant in self.hold_variants if variant.name == self.variant), None)

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
            enter_order=spec.enter_order, restore_order=spec.restore_order,
            peak_first={**self.peak_first, ACTION_GRID_CHARGE: spec.stop_first},
            requires=(*self.requires, *((role.name, states, why) for role, states, why in spec.requires)),
            # What it owns it writes itself; nothing of them is left to watch.
            watched=tuple(row for row in self.watched if row[0].name not in owned),
            own_states={**self.own_states, **spec.own_states},
            phased=True,
        )

    def supports(self, action: str) -> bool:
        return action == ACTION_SELF_CONSUME or action in self.states

    def export_signed_by_charge(self) -> bool:
        """Whether a discharge to the grid writes its rate to a number the grid charge also writes its rate to, with
        the other sign (a signed power target). The verification's house step writes no rate on any brand (the
        plant's own mode), so only the charge step shows which sign discharges: a wrong sign would charge from the
        grid at the full rate. `gate.allowed_actions` then needs `charge` passed for an export."""
        charge, export = self.states.get(ACTION_GRID_CHARGE), self.states.get(ACTION_EXPORT_DISCHARGE)
        if charge is None or export is None:
            return False

        def rated(build: Callable[[float, int], tuple[Want, ...]]) -> dict[str, float]:
            one = {want.role: want.value for want in build(1.0, WATCHDOG_S)}
            two = {want.role: want.value for want in build(2.0, WATCHDOG_S)}
            return {
                name: float(value) for name, value in one.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value != two.get(name)
            }

        charged = rated(charge)
        return any(name in charged and value * charged[name] < 0 for name, value in rated(export).items())

    def wanted(
        self, action: str, rate_kw: float, watchdog_s: int = WATCHDOG_S, phases: Phases | None = None, *,
        house_kw: float | None = None, behind_kw: Phases | None = None, soc_pct: float | None = None,
        rated_kw: float | None = None, absent: frozenset[str] = frozenset(), capacity_kwh: float | None = None,
    ) -> dict[str, Native]:
        """Every restored role's wanted value for the action (`SNAPSHOT` where the action leaves it). `phases`: a
        grid charge per phase on a phased control (kW on L1, L2, L3, negative for a discharge), instead of the
        brand's own charge at `rate_kw`; `behind_kw`: the load behind each phase's point, for a `grid` target.
        `house_kw`: the house's net load now, for an action the brand runs by the grid's power (`house_states`).
        `soc_pct`, `rated_kw` and `capacity_kwh` fill in what depends on the plant (`SOC_FLOOR`, `SOC_DEPTH`,
        `pct_of_rated`, `soc_ceiling`); `absent` are optional roles the plant does not have."""
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
                wanted[want.role] = _resolve(
                    want.value, soc_pct, rated_kw, self.soc_floor_margin_pct, capacity_kwh, self.charge_block_s
                )
        for name in absent:
            wanted.pop(name, None)
        return wanted

    def unresolved(self, wanted: Mapping[str, Native]) -> str | None:
        """Why a wanted state cannot be written yet: `soc_unknown`, `rated_unknown` or `size_unknown` while a value
        that depends on the plant's reading has none."""
        for value in wanted.values():
            if value in (SOC_FLOOR, SOC_DEPTH):
                return "soc_unknown"
            if isinstance(value, str) and value.startswith(_PCT_PREFIX):
                return "rated_unknown"
            if isinstance(value, str) and value.startswith(_CEILING_PREFIX):
                return "size_unknown"
        return None

    def optional_names(self) -> frozenset[str]:
        return frozenset(role.name for role in self.roles if role.optional)


def native(role: Role, kw: float) -> float:
    """A power in the role's unit, rounded down to its step (never above what was asked)."""
    value = max(kw, 0.0) * role.scale
    if role.step:
        value = math.floor(value / role.step + 1e-9) * role.step
    return round(value, 6)


def pct_of_rated(kw: float) -> str:
    """A power in kW as a share of the battery's rated power, for a role in percent (GoodWe's eco power): resolved by
    `BrandControl.wanted` with the rating. A negative power is the same share, negative (Fronius's discharge limit
    below 0 forces a charge)."""
    return f"{_PCT_PREFIX}{kw!r}>"


def soc_ceiling(kw: float) -> str:
    """A SoC ceiling (%) for a charge at `kw`: what the battery reaches in one block (`BrandControl.charge_block_s`)
    at that rate from the SoC now, rounded up, at most 100; resolved by `BrandControl.wanted` with the SoC and the
    battery's size."""
    return f"{_CEILING_PREFIX}{kw!r}>"


def _resolve(
    value: Native, soc_pct: float | None, rated_kw: float | None, margin_pct: float = 0.0,
    capacity_kwh: float | None = None, block_s: float | None = None,
) -> Native:
    """A value that depends on the plant, filled in; itself when the reading it needs is missing. A floor at the SoC
    is rounded down and kept `margin_pct` below it (never above the SoC); a ceiling is one block ahead of it."""
    if isinstance(value, str) and value.startswith(_CEILING_PREFIX):
        if (
            soc_pct is None or not math.isfinite(soc_pct) or capacity_kwh is None or not math.isfinite(capacity_kwh)
            or capacity_kwh <= 0 or block_s is None or block_s <= 0
        ):
            return value
        kw = float(value[len(_CEILING_PREFIX):-1])
        step = math.ceil(max(kw, 0.0) * block_s / 3600.0 / capacity_kwh * 100.0 - 1e-9)
        return float(min(math.floor(min(max(soc_pct, 0.0), 100.0) + 1e-9) + max(step, 1), 100))
    if value == SOC_FLOOR or value == SOC_DEPTH:
        if soc_pct is None or not math.isfinite(soc_pct):
            return value
        level = float(math.floor(min(soc_pct, 100.0) + 1e-9)) - max(math.ceil(margin_pct - 1e-9), 0)
        floor = max(level, 1.0 if value == SOC_DEPTH else 0.0)
        return floor if value == SOC_FLOOR else 100.0 - floor
    if isinstance(value, str) and value.startswith(_PCT_PREFIX):
        if rated_kw is None or not math.isfinite(rated_kw) or rated_kw <= 0:
            return value
        kw = float(value[len(_PCT_PREFIX):-1])
        share = float(min(max(math.floor(abs(kw) / rated_kw * 100.0 + 1e-9), 1), 100))
        # A negative power is a negative share (Fronius's forcing limits), its size rounded down as a positive one.
        return -share if kw < 0 else share
    return value


def fixed_rate_kw(control: BrandControl, facts: Any) -> float | None:
    """The rate a plant charges at when the recipe cannot set it (`BrandControl.fixed_charge`), in kW: the larger of
    its own rating (`facts.max_charge_kw`) and the recipe's figure per unit of battery size. Infinite while the
    battery's size is not read (the number of units is then unknown: two Powerwalls are not checked as one), so every
    such charge is refused. `None` for a control that sets its rate."""
    if not control.fixed_charge:
        return None
    capacity = getattr(facts, "capacity_kwh", None)
    if not isinstance(capacity, (int, float)) or isinstance(capacity, bool) or not math.isfinite(capacity) or capacity <= 0:
        return math.inf
    units = max(1, math.ceil(capacity / control.fixed_unit_kwh - 0.15))
    rating = getattr(facts, "max_charge_kw", None)
    return max(control.fixed_charge_kw * units, rating if isinstance(rating, (int, float)) else 0.0)


#: The `action_requires` reasons that are the person's own setting on the plant, told in the status (`needs`): only
#: they can change it, and until they do the action never runs.
TOLD_ACTION_REASONS: Final = frozenset({"inverter_power_zero"})


def forbidden_read(state: str | None, forbidden: frozenset[str]) -> bool:
    """Whether a role's state is one of an action's `forbidden` states (`BrandControl.action_requires`): the same
    text, or the same number where both are numbers. No state at all is forbidden too (nothing shows it is not)."""
    if state is None or state in forbidden:
        return True
    try:
        value = float(state)
    except ValueError:
        return False
    for item in forbidden:
        try:
            if math.isfinite(value) and float(item) == value:
                return True
        except ValueError:
            continue
    return False


def reading(role: Role, state: str | None) -> str | None:
    """A role's state as compared: normalised, `None` when it is no reading."""
    if role.normalize is not None:
        return role.normalize(state)
    if state is None or state.strip().lower() in _NO_READING:
        return None
    return state


def answered(role: Role, state: str | None, attributes: Mapping[str, Any] | None) -> str | None:
    """The state a role is read by (before `reading`). For a role whose state shows the last request whoever sent it
    and whose device answers each request in the attributes (`Role.answer`), the state counts only once the device
    took that request: while the answer is awaited, and after a refusal, the read is `unavailable`, no evidence, so a
    write is never confirmed by its own request (the ledger waits, then retries or hands back) and a refused one is
    never someone else's change. Without a transaction in the attributes (no request seen since the integration
    started; a restart does not restore the attributes) the state is read as it is."""
    if (
        role.follows_max is not None and attributes and attributes.get(role.follows_max) is True
        and state is not None and state.strip().lower() not in _NO_READING
    ):
        # The number follows its own maximum, whatever it shows (`Role.follows_max`).
        return FOLLOWS_MAX
    spec = role.answer
    if spec is None or state is None or not attributes or attributes.get(spec.transaction) is None:
        return state
    answer = attributes.get(spec.status)
    return state if isinstance(answer, str) and answer.strip().lower() in spec.taken else "unavailable"


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


# --- Ferroamp's last request ---------------------------------------------------------------------------------

_FERROAMP_RUN = re.compile(r"^(charge|discharge) \((\d+(?:\.\d+)?)\)$")


def ferroamp_last_cmd(state: str | None) -> str | None:
    """"charge (1000)" -> `charge:1000`, "auto" -> `auto`; a sensor that never saw a request ("unknown") reads
    `auto`, the hub's own state; `None` for anything else (unavailable, a command not known)."""
    text = "" if state is None else state.strip().lower()
    if text in ("", "unknown", "none", "auto"):
        return "auto"
    found = _FERROAMP_RUN.match(text)
    return None if found is None else f"{found.group(1)}:{int(float(found.group(2)))}"


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
#: follows V2.9; ignored where it follows V2.7 (the battery then also stores a sun surplus, as in self-consumption).
SIGENERGY_OUT_OF_SCOPE_V27: Final = frozenset({(ACTION_DISCHARGE_HOUSE, "charge_limit")})


def _sig(mode: str, **limits: Callable[[float], Native]) -> Callable[[float, int], tuple[Want, ...]]:
    def build(rate_kw: float, _watchdog_s: int) -> tuple[Want, ...]:
        wants = [Want(name, fn(rate_kw)) for name, fn in limits.items()]
        return (*wants, Want("remote_ems", True), Want("control_mode", mode))

    return build


#: Whether Sigenergy's hold follows a sun surplus with its charge limit (`SIGENERGY_HOLD_SURPLUS`). Off until the
#: owner's on-site `pv_first_surplus` step (`tools/sigen_hold_peak_test.py`) has passed in sunshine (at least
#: 0.5 kW of surplus for 5 minutes); until then the hold keeps the charge limit at 0, the proven hold, and a sun
#: surplus is exported. One line; the recipe and the executor need nothing else.
SIGENERGY_SURPLUS_TRACKING: Final = False
#: The surplus follow of Sigenergy's hold (owner decision 2026-11-23, variant B): mode 4 charges at its charge limit
#: from the sun first, so a limit at the sun's surplus stores it and takes nothing from the grid. A limit left
#: standing when Home Assistant stops and the sun goes grid-charges at that limit (on site 2026-11-23,
#: `pv_first_stale_charge`: 1.0 kW, as written), and the import limit (40040) still takes the plant down to it (it
#: discharged and held 0.20 kW a phase at 0.6 kW): that costs money, never the fuse.
SIGENERGY_HOLD_SURPLUS: Final = HoldSurplus("charge_limit", deadband_kw=0.4, step_kw=0.1, raise_gap_s=60.0)

#: Sigenergy's hold: mode 4, "Command Charging (PV First)", with the charge limit at 0 (owner decision 2026-11-23,
#: variant B, the only hold). Nothing charges or discharges, a sun surplus is exported (stored once the surplus follow
#: is on, `SIGENERGY_SURPLUS_TRACKING`). It keeps to both protocol generations: the charge limit acts in mode 4 under
#: V2.7 and V2.9. It also keeps the plant's own peak cover with Home Assistant down: on site 2026-11-23 (SPC117, sigen
#: 2.0.3, no sun, house ~2.1 kW) the import limit (40040) lowered to 0.6 kW made the plant discharge 1.5-3.2 kW within
#: ~25-30 s and hold the grid at 0.60 kW, 0.20 kW on every phase, also through a 1.6 kW step on B. The hold it replaced,
#: Maximum Self Consumption with the discharge limit at 0 (`msc_no_discharge`), failed that test: the discharge limit
#: won over the import limit and the grid took the whole house. While Home Assistant runs, the peak cover writes the
#: mode back to Maximum Self Consumption, where the plant balances each phase itself.
SIGENERGY_HOLDS: Final = (
    HoldVariant(
        "pv_first", _sig(_SIG_PV_FIRST, charge_limit=lambda _rate: 0.0), ("control_mode",),
        "Command Charging (PV First) with the charge limit at 0", idle=True,
        surplus=SIGENERGY_HOLD_SURPLUS if SIGENERGY_SURPLUS_TRACKING else None,
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

#: Sigenergy's grid import limit held per phase (`plans/v3_sigen_local_fuse_protection.md`): with independent phase
#: power control on (40030), the owner's plant held each phase at limit / 3 once the limit bound, in AI mode (12 days,
#: 13.8 kW: 4.60 kW a phase) and in Remote EMS "Command Charging (Grid First)" (2026-11-18, 6 kW: 2.00 kW a phase,
#: putting out 0.33 kW on the loaded phase). The on-site test of 2026-11-21 (`tools/sigen_import_limit_test.py`;
#: SigenStor EC 12.0 TP, firmware V100R001C21SPC117, sigen 2.0.3, Remote EMS "Command Charging (Grid First)", 40030
#: on, Output Type L1/L2/L3/N, grid sensor online) showed the rest: at 6 kW a kettle on B held at 2.0 kW at full
#: charge and again after ten minutes with nothing written to the plant; at 8 kW the same at full charge, and
#: **with the total under the limit** (6.0-6.26 kW of 8 kW, a 1 kW charge) B held at 2.58-2.73 kW with 3.3 kW of
#: house on it, the plant putting out 0.67 kW on B and charging 1.0 kW on A and C: a per-phase hold, not a total
#: one. Recorded as `field_tested` (owner, 2026-11-21); each site still says its plant's limit acts
#: (`battery_control_import_limit_acts`) and every reading is checked again at each start and look. The limit
#: regulates on the
#: plant's grid sensor (V2.9 Table 5-2-1, "Grid Sensor needed"): its status, 30004, is TypQxQ 2.0.3
#: `static_sensor.py` key `plant_grid_sensor_status` (states `offline`/`online`, disabled by default).
SIGENERGY_IMPORT_LIMIT_PHASES: Final = ImportLimitPhases(
    switch=_SIG_PHASE_SWITCH.match,
    grid_sensor=_key("plant_grid_sensor_status"),
    sensor_online="online",
    # V2.9: independent phase power control is "valid only when the output type is L1/L2/L3/N".
    output_type="l1_l2_l3_n",
    # The owner's on-site test passed on 2026-11-21 (see above).
    field_tested=True,
)

#: Where Sigenergy's import limit was shown to act (see `SIGENERGY_IMPORT_LIMIT_PHASES`).
SIGENERGY_IMPORT_LIMIT_TRUST: Final = ImportLimitTrust(
    acts_in=frozenset({ACTION_GRID_CHARGE}), firmware=frozenset({"V100R001C21SPC117"}),
)

SIGENERGY_CONTROL: Final = BrandControl(
    brand="sigenergy",
    # Flips to True once the owner's on-site tests are rerun on the reworked executor (one line, here and below).
    field_tested=False,
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
    import_limit_phases=SIGENERGY_IMPORT_LIMIT_PHASES,
    # The on-site tests of 2026-11-18 and 2026-11-21 ran in Remote EMS "Command Charging (Grid First)", this recipe's
    # grid charge, on firmware V100R001C21SPC117 (owner decision 2026-11-23: trusted on the firmware it was tested
    # on; another is added once it is re-tested there). The site's own option that its limit acts is not tied to it.
    import_limit_trust=SIGENERGY_IMPORT_LIMIT_TRUST,
    note="Remote EMS replaces the app's mode only while it is on; SpotNav takes a plant in Maximum Self"
    " Consumption only, so turning it off hands back the mode it found.",
    hold_variants=SIGENERGY_HOLDS,
    variant=SIGENERGY_HOLDS[0].name,
    # 40031 keeps its last value while Remote EMS is off, and the mode select is unavailable then (it cannot be
    # written before the switch): both limits at 0 make a Command Charging or Discharging mode left there do nothing
    # (V2.7: 40032 acts in 3 and 4, 40034 in 5 and 6; V2.9: everywhere) until the mode is written.
    prime={"charge_limit": 0.0, "discharge_limit": 0.0},
    # Both limits act in the Command modes (V2.7) or everywhere (V2.9): lowered before a mode changes, raised after.
    mode_limits=("charge_limit", "discharge_limit"),
    phase_control=SIGENERGY_PHASES,
    # A target left standing would run in the seconds of PCS Remote Control a Remote EMS on passes through.
    watched=tuple((role, 0.0, "stale_target") for role in (_SIG_TOTAL_TARGET, *_SIG_PHASE_TARGETS)),
    # The protocol is silent on how often the dispatch registers may be written (see the module): the moderate cap.
    write_caps={"setpoint": SETPOINT_WRITES_UNSTATED},
)

#: An inverter's Output Type (Sigenergy's enum, lower-cased) as the phases it feeds.
OUTPUT_TYPE_PHASES: Final[Mapping[str, int]] = {"l_n": 1, "l1_l2_n": 2, "l1_l2_l3": 3, "l1_l2_l3_n": 3}


def plant_phases(control: BrandControl, state: str | None) -> int:
    """The phases a plant spreads a grid charge over, from its `output_type` entity's `state`: the recipe's own
    reading of it (`output_phases`) or Sigenergy's enum; the recipe's `grid_charge_phases` when it reads nothing or
    nothing known."""
    if not state:
        return control.grid_charge_phases
    if control.output_phases is not None:
        read = control.output_phases(str(state))
    else:
        read = OUTPUT_TYPE_PHASES.get(str(state).lower())
    return control.grid_charge_phases if read is None else read

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
    # Not field-tested on a plant of the brand: writes only after a passed hardware test (a plant whose control
    # already ran keeps it, `GATED_AFTER_RUNNING`).
    hardware_test_first=True,
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
    # The command timeout returns the inverter to its default mode (the maker's word; not measured).
    expiry=Expiry("device_deadman", "spotnav", float(WATCHDOG_S), ("command_timeout", "command_mode")),
    requires=(("control_mode", frozenset({"Remote Control"}), "not_in_remote_control"),),
    peak_first={ACTION_HOLD: ("command_mode",), ACTION_GRID_CHARGE: ("command_mode",)},
    action_requires=((ACTION_GRID_CHARGE, "ac_charge_policy", frozenset({"Disabled"}), "ac_charge_disabled"),),
    note="Remote Control only, as the owner set it; the command timeout returns the inverter to its default mode.",
)

_HW_FORCIBLE = Role(
    "forcible", "service", _key("forcible_charge_discharge_write"), restored=False,
    services={"charge": "huawei_solar.forcible_charge", "discharge": "huawei_solar.forcible_discharge",
              "stop": "huawei_solar.stop_forcible_charge"},
    normalize=huawei_forcible, lease=True,
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
    # Not field-tested on a plant of the brand: writes only after a passed hardware test (a plant whose control
    # already ran keeps it, `GATED_AFTER_RUNNING`).
    hardware_test_first=True,
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
    # A forcible charge carries its duration (the maker's word; not measured).
    expiry=Expiry("device_lease", "spotnav", float(WATCHDOG_S), ("forcible",)),
    requires=(("forcible", frozenset({"stop"}), "forcible_charge_running"),),
    peak_first={ACTION_HOLD: ("discharge_power",), ACTION_GRID_CHARGE: ("forcible",)},
    note="Services and numbers need the installer password; a forcible charge ends by itself after its duration.",
)

# The select and the numbers are the integration's own local values (sent on the trigger): read back as its echo.
_SX_CONTROL = Role(
    "power_control", "select", _key("remotecontrol_power_control", "select"), readback="echo",
)
_SX_POWER = Role(
    "active_power", "number", _key("remotecontrol_active_power", "number"), scale=1000.0, step=100.0, readback="echo"
)
_SX_REPEAT = Role(
    "autorepeat", "number", _key("remotecontrol_autorepeat_duration", "number"), step=1.0, readback="echo"
)
_SX_TRIGGER = Role("trigger", "button", _key("remotecontrol_trigger", "button"), restored=False, readback="none")


def _sx(option: str, sign: float = 0.0) -> Callable[[float, int], tuple[Want, ...]]:
    def build(rate_kw: float, watchdog_s: int) -> tuple[Want, ...]:
        wants = [Want("autorepeat", float(watchdog_s)), Want("power_control", option)]
        if sign:
            wants.append(Want("active_power", sign * native(_SX_POWER, rate_kw)))
        return (*wants, Want("trigger", True))

    return build


SOLAX_CONTROL: Final = BrandControl(
    brand="solax",
    # Not field-tested on a plant of the brand: writes only after a passed hardware test (a plant whose control
    # already ran keeps it, `GATED_AFTER_RUNNING`).
    hardware_test_first=True,
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
    # The remote control's autorepeat ends after its duration (the maker's word; not measured).
    expiry=Expiry("device_lease", "spotnav", float(WATCHDOG_S), ("trigger",)),
    requires=(("power_control", frozenset({"Disabled"}), "remote_control_in_use"),),
    peak_first={ACTION_HOLD: ("power_control", "trigger"), ACTION_GRID_CHARGE: ("power_control", "trigger")},
    note="The integration holds the values and sends them on the trigger; only the battery's power shows the"
    " effect.",
)

_SOL_GENERAL: Final = "General"  # 0x0101: the plant's own self-consumption
_SOL_BATTCTRL: Final = "EMS BattCtrl"  # 0x0303: the battery follows 50207
_SOL_ACCTRL: Final = "EMS ACCtrl"  # 0x0301: the inverter's AC power follows 50203 or 50204-50206
_SOL_EACH_PHASE: Final = "Power Setting on each Phase"  # 50202 = 2

# Every Solinteg role waits `READBACK_STALL_S` for its read-back: the dongle's link stalls now and then.
_SOL_MODE = Role(
    "working_mode", "select", _key("working_mode", "select"), restored=False, readback_s=READBACK_STALL_S
)
# 50207: the battery's power target (kW, negative charges). Handed back as 0, never as the value found.
_SOL_TARGET = Role(
    "battery_target", "number", _key("battery_charge_discharge_power_target", "number"), step=0.01, restored=False,
    write_class="setpoint", readback_s=READBACK_STALL_S,
)
# 50202: written only by the control per phase, and left as written (it acts only in EMS ACCtrl).
_SOL_AC_MODE = Role(
    "acctrl_mode", "select", _key("ems_acctrl_mode", "select"), restored=False, readback_s=READBACK_STALL_S
)
_SOL_UNBALANCED: Final = Role(
    "unbalanced_output", "select", _key("grid_unbalanced_output", "select"), restored=False,
    readback_s=READBACK_STALL_S,
)
_SOL_PHASE_TARGETS: Final = (
    Role("phase_l1_target", "number", _key("ems_acctrl_phase_a_power", "number"), step=0.01, restored=False,
         write_class="setpoint", readback_s=READBACK_STALL_S),
    Role("phase_l2_target", "number", _key("ems_acctrl_phase_b_power", "number"), step=0.01, restored=False,
         write_class="setpoint", readback_s=READBACK_STALL_S),
    Role("phase_l3_target", "number", _key("ems_acctrl_phase_c_power", "number"), step=0.01, restored=False,
         write_class="setpoint", readback_s=READBACK_STALL_S),
)


def _sol(mode: str, sign: float = 0.0) -> Callable[[float, int], tuple[Want, ...]]:
    """The working mode and the battery's target: `sign` -1 charges at the rate, 1 discharges, 0 is a target of 0."""

    def build(rate_kw: float, _watchdog_s: int) -> tuple[Want, ...]:
        size = native(_SOL_TARGET, rate_kw) if sign else 0.0
        return (Want("working_mode", mode), Want("battery_target", 0.0 if size == 0 else round(sign * size, 6)))

    return build


#: Solinteg's one hold: EMS BattCtrl with the battery's target at 0. The battery stands idle (the sun's surplus is
#: exported, not stored); its peak cover writes the mode back to General, where the plant covers the house itself.
SOLINTEG_HOLDS: Final = (
    HoldVariant(
        "battctrl_idle", _sol(_SOL_BATTCTRL), ("working_mode",), "EMS BattCtrl with the battery's power target at 0",
        idle=True,
    ),
)

#: Solinteg's targets per phase (see the module; never run on a device, and not usable until the plugin reads the
#: inverter's power per phase): phase A, B, C taken as L1, L2, L3, a negative target charges. Grid Unbalanced Output
#: (50006) is read, never written. The targets first, then the scheduling mode, then the working mode; the battery's
#: own target (50207) is kept at 0 beside them. Every hand-back and change of action writes the working mode first:
#: one write stops the charge on all three phases.
SOLINTEG_PHASES: Final = PhaseControl(
    targets=_SOL_PHASE_TARGETS,
    charge_sign=-1.0,
    follow=(
        Want("battery_target", 0.0), Want("acctrl_mode", _SOL_EACH_PHASE), Want("working_mode", _SOL_ACCTRL),
    ),
    target_kind="plant",
    modes=(_SOL_AC_MODE,),
    requires=((_SOL_UNBALANCED, frozenset({"on"}), "unbalanced_output_off"),),
    enter_order=(
        "phase_l1_target", "phase_l2_target", "phase_l3_target", "acctrl_mode", "working_mode", "battery_target",
    ),
    restore_order=("working_mode", "phase_l1_target", "phase_l2_target", "phase_l3_target", "battery_target"),
    stop_first=("working_mode",),
)

SOLINTEG_CONTROL: Final = BrandControl(
    brand="solinteg",
    # Not field-tested on a plant of the brand: writes only after a passed hardware test (a plant whose control
    # already ran keeps it, `GATED_AFTER_RUNNING`).
    hardware_test_first=True,
    adapter=SOLINTEG,
    roles=(_SOL_MODE, _SOL_TARGET),
    states={
        ACTION_HOLD: SOLINTEG_HOLDS[0].build,
        ACTION_GRID_CHARGE: _sol(_SOL_BATTCTRL, -1.0),
        # No EMS mode covers the house and stores nothing: the plant's own mode does the first.
        ACTION_DISCHARGE_HOUSE: _sol(_SOL_GENERAL),
        ACTION_EXPORT_DISCHARGE: _sol(_SOL_BATTCTRL, 1.0),
    },
    # The mode before the target: a value written before the mode switch has been seen not to act. The target reads
    # 0 until then (`watched`, and every hand-back leaves it so).
    enter_order=("working_mode", "battery_target"),
    restore_order=("working_mode", "battery_target"),
    own_states={"working mode": frozenset({_SOL_BATTCTRL, _SOL_ACCTRL})},
    requires=(("working_mode", frozenset({_SOL_GENERAL}), "not_in_general"),),
    # General releases the battery in one write: a hold's peak cover and a grid charge's stop.
    peak_first={ACTION_HOLD: SOLINTEG_HOLDS[0].peak_first, ACTION_GRID_CHARGE: ("working_mode",)},
    # Nothing states how a BattCtrl charge is spread over the phases, and no import limit is counted on (see the
    # module): the HA-down cap takes the whole charge on one phase.
    grid_charge_phases=1,
    note="EMS BattCtrl with the battery's power target; back to General. Never run on a device; the inverter keeps"
    " the last command if Home Assistant stops.",
    hold_variants=SOLINTEG_HOLDS,
    variant=SOLINTEG_HOLDS[0].name,
    phase_control=SOLINTEG_PHASES,
    # A target left standing would run in the seconds between the mode's write and the target's own.
    watched=((_SOL_TARGET, 0.0, "stale_target"),),
    write_caps={"setpoint": SETPOINT_WRITES_UNSTATED},
)

_SOLINTEG_NORMAL: Final = {
    "working_mode": _SOL_GENERAL, "battery_target": 0.0, **{role.name: 0.0 for role in _SOL_PHASE_TARGETS},
}

# --- Victron ESS (see the module) ---------------------------------------------------------------------------------

# -1 is "no limit" and 0 is "none at all", one watt apart on entities whose step is 50: read to the watt.
_VIC_CHARGE = Role(
    "charge_power", "number", _key("system_ess_max_charge_power", "number"), scale=1000.0, step=50.0, tolerance=1.0
)
_VIC_DISCHARGE = Role(
    "discharge_power", "number", _key("system_ess_max_inverter_power_limit", "number"), scale=1000.0, step=50.0,
    tolerance=1.0,
)
#: The GX's own default grid setpoint (W), what a sale found at the snapshot is restored as, and the setpoint (W)
#: below which a found value is a sale, not a person's small bias against importing.
VICTRON_GRID_DEFAULT_W: Final = 50.0
VICTRON_SALE_BELOW_W: Final = -200.0
_VIC_GRID = Role(
    "grid_setpoint", "number", _key("system_ac_power_set_point", "number"), scale=1000.0, step=10.0, tolerance=1.0,
    restored_below=(VICTRON_SALE_BELOW_W, VICTRON_GRID_DEFAULT_W),
)
_VIC_MODE = Role("ess_mode", "select", _key("system_ess_mode", "select"))
#: The two Hub4 flags are written off for a charge per phase and never handed back: outside external control the GX
#: sets them itself, by Victron's documentation; no plant has shown it yet, so the first field report checks it
#: (`docs/battery-brands.md`).
_VIC_NO_CHARGE = Role("disable_charge", "switch", _key("vebus_hub4_disable_charge", "switch"), restored=False)
_VIC_NO_FEED_IN = Role("disable_feed_in", "switch", _key("vebus_hub4_disable_feed_in", "switch"), restored=False)
_VIC_EXTERNAL: Final = "external_control"  # Hub4Mode 3
#: The Multi falls back to passthru this long after the last write of a setpoint (s), by Victron's page, and how often
#: each is renewed. The fallback is not yet seen on a plant, so the recipe's expiry is stated, not measured (see the
#: module for the field test that must come first).
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
#: mode back as found. Each setpoint is renewed well inside the Multi's 60 s watchdog; that the Multi goes to
#: passthru by itself when the writes stop is the page's word only, so the HA-down cap is not lifted on it.
VICTRON_PHASES: Final = PhaseControl(
    targets=_VIC_PHASE_TARGETS,
    charge_sign=1.0,
    follow=(Want("ess_mode", _VIC_EXTERNAL), Want("disable_charge", False), Want("disable_feed_in", False)),
    target_kind="grid",
    roles=(_VIC_MODE, _VIC_NO_CHARGE, _VIC_NO_FEED_IN),
    park=False,
    behind=_vic_phase_roles("load", "vebus_inverter_output_power"),
    point=_vic_phase_roles("input", "vebus_inverter_input_power"),
    # The Multi's 60 s watchdog, renewed every `VICTRON_RENEW_S`: the page's word only, so stated, not measured (the
    # module): the targets are reckoned as if they stay until a field test shows the fallback.
    expiry=Expiry(
        "device_deadman", "spotnav", float(VICTRON_SILENCE_S), tuple(role.name for role in _VIC_PHASE_TARGETS),
        renew_s=VICTRON_RENEW_S,
    ),
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
    # Not field-tested on a plant of the brand: writes only after a passed hardware test (a plant whose control
    # already ran keeps it, `GATED_AFTER_RUNNING`).
    hardware_test_first=True,
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
    # "Max inverter power" at 0 is the person's (or an installer's) "never discharge": the Multi feeds nothing, so a
    # discharge would write and then show no effect. SpotNav never writes it up; the status says so (`needs`).
    action_requires=tuple(
        (action, "discharge_power", frozenset({"0"}), "inverter_power_zero")
        for action in (ACTION_DISCHARGE_HOUSE, ACTION_EXPORT_DISCHARGE)
    ),
    note="Mode 2 for a hold, a balanced charge and a discharge (settings on the GX, written rarely); external"
    " control with a setpoint per phase, renewed inside the Multi's 60 s watchdog, for a charge per phase. Not"
    " tested on a plant.",
    phase_control=VICTRON_PHASES,
    write_caps={"setpoint": SETPOINT_WRITES_WATCHED},
)

# No `unset_as_rating`: a limit never set (`unknown`) has no value to hand back, and the plant's maximum is never
# written in its place (a grid operator's export cap must never be overwritten; owner decision 2026-11-25). The
# integration has no entity that turns the limitation off again, so such a plant is not curtailed
# (`no_restore_value:export_limit`).
_SIG_EXPORT = Role(
    "export_limit", "number", _key("plant_grid_point_maximum_export_limitation", "number"), step=0.001,
    # Without `unset_as_rating` this writes nothing: it only says what an `unknown` here means (the card's line).
    unknown_is_unset=True,
)

SIGENERGY_EXPORT_CONTROL: Final = BrandControl(
    brand="sigenergy",
    # Flips to True once the owner's on-site tests are rerun on the reworked executor (one line, here and below).
    field_tested=False,
    adapter=SIGENERGY,
    roles=(_SIG_EXPORT,),
    states={ACTION_CURTAIL_EXPORT: lambda _rate, _w: (Want("export_limit", 0.0),)},
    enter_order=("export_limit",),
    restore_order=("export_limit",),
    # The battery control's own Remote EMS is not "managed by its own system" for the export limit either.
    own_states={"EMS work mode": frozenset({"remote_ems"})},
    takeover={"EMS work mode": frozenset({"ai_mode"})},
    kind="export",
    limit_point="grid",
    # The integration's own table: "the most power your installation can send to the public grid", 0 to 100 kW.
    zero_stated=True,
    note="Grid Export Limitation (40038) applies in every EMS mode and needs the plant's grid sensor. Handed back"
    " exactly as found; a limit never set (`unknown`) cannot be handed back, so it is not curtailed, and the"
    " plant's maximum is never written.",
)

# --- GoodWe (the HACS fork) ---------------------------------------------------------------------------------------

# The numbers and the select do not poll, so only a reload shows a refused restore: Home Assistant's echo, watched
# after a hand-back as any echo is.
_GW_MODE = Role("operation_mode", "select", _goodwe("operation_mode", "select"), readback="echo", settle_s=900.0)
_GW_ECO_POWER = Role(
    "eco_power", "number", _goodwe("eco_mode_power", "number"), step=1.0, readback="echo", settle_s=900.0
)
_GW_ECO_SOC = Role("eco_soc", "number", _goodwe("eco_mode_soc", "number"), step=1.0, readback="echo", settle_s=900.0)
_GW_DEPTH = Role(
    "depth_of_discharge", "number", _goodwe("battery_discharge_depth", "number"), step=1.0, readback="echo",
    settle_s=900.0,
)


def _gw_charge(rate_kw: float, _watchdog_s: int) -> tuple[Want, ...]:
    # Numbers first, the mode last: the select sends the eco group when it changes to eco_charge. The SoC ceiling is
    # the inverter's full: some firmwares ignore it anyway (HACS #466, #486), so the plan's slot ends the charge.
    return (
        Want("eco_soc", 100.0), Want("eco_power", pct_of_rated(rate_kw)), Want("operation_mode", "eco_charge"),
    )


GOODWE_CONTROL: Final = BrandControl(
    brand="goodwe",
    # Not field-tested on a plant of the brand: writes only after a passed hardware test (a plant whose control
    # already ran keeps it, `GATED_AFTER_RUNNING`).
    hardware_test_first=True,
    adapter=GOODWE,
    roles=(_GW_ECO_SOC, _GW_ECO_POWER, _GW_DEPTH, _GW_MODE),
    states={
        # Unverified: the cut-off at the SoC now (see the module).
        ACTION_HOLD: lambda _rate, _w: (Want("depth_of_discharge", SOC_DEPTH),),
        ACTION_GRID_CHARGE: _gw_charge,
        # Plain self-consumption covers the house: everything at what was found, in `general`.
        ACTION_DISCHARGE_HOUSE: lambda _rate, _w: (Want("operation_mode", "general"),),
    },
    enter_order=("eco_soc", "eco_power", "depth_of_discharge", "operation_mode"),
    restore_order=("operation_mode", "eco_power", "eco_soc", "depth_of_discharge"),
    # The adapter's mode rule is free only in `general`: the grid charge's own `eco_charge` is SpotNav's, not the
    # plant's own management, while SpotNav runs it (one found there at the start is still refused).
    own_states={"operating mode": frozenset({"eco_charge"})},
    # A grid charge stops with the mode back to general; no peak cover under a hold (the cut-off is a stored setting).
    peak_first={ACTION_GRID_CHARGE: ("operation_mode",)},
    hacs_roles=("eco_power", "eco_soc"),
    hacs_repo="mletenay/home-assistant-goodwe-inverter",
    # Unconfirmed whether the inverter charges from the grid to a cut-off at the SoC: a hold that does is handed back.
    hold_grid_guard=True,
    note="Needs the HACS integration (mletenay/home-assistant-goodwe-inverter): core has no eco charge mode or eco"
    " numbers. The eco SoC ceiling is ignored on some firmwares, so a charge is judged by the battery's power.",
)

# --- Tesla Powerwall (cloud, slow) --------------------------------------------------------------------------------

# tesla_fleet echoes a write at once and polls the cloud every 60 s: the read-back waits for the cloud, and fifteen
# polls after a hand-back show a refused restore.
_TS_RESERVE = Role(
    "reserve", "number", _tesla("backup_reserve_percent", "number"), step=1.0, readback="echo", readback_s=420.0,
    settle_s=900.0,
)
#: Inverted by the integration: on means grid charging is allowed. Only on a site with battery and solar.
_TS_GRID = Role(
    "grid_charging", "switch", _tesla("components_disallow_charge_from_grid_with_solar_installed", "switch"),
    optional=True, readback="echo", readback_s=420.0, settle_s=900.0,
)
#: Tesla's cloud is slow and its energy settings may be limited to a few writes a day (unconfirmed): few writes.
TESLA_WRITES_PER_DAY: Final = 24
#: A Powerwall grid charge is commanded in blocks this long (s; `BrandControl.charge_block_s`): at 5 kW per 13.5 kWh
#: one block is at most about 19 points of charge left standing if nothing comes back.
TESLA_CHARGE_BLOCK_S: Final = 1800.0

TESLA_CONTROL: Final = BrandControl(
    brand="tesla",
    # Not field-tested on a plant of the brand: writes only after a passed hardware test (a plant whose control
    # already ran keeps it, `GATED_AFTER_RUNNING`).
    hardware_test_first=True,
    adapter=TESLA,
    roles=(_TS_RESERVE, _TS_GRID),
    states={
        # Grid charging off first (restored on the hand-back), then the reserve under the level now: a reserve above
        # the SoC charges from the grid, so it is rounded down and kept `soc_floor_margin_pct` below a SoC that the
        # cloud reports up to a minute late.
        ACTION_HOLD: lambda _rate, _w: (Want("grid_charging", False), Want("reserve", SOC_FLOOR)),
        # Grid charging allowed and the reserve one block ahead of the level (renewed each block): with Home Assistant
        # or the cloud gone the Powerwall charges at most that block from the grid, not to 100 %.
        ACTION_GRID_CHARGE: lambda rate, _w: (Want("grid_charging", True), Want("reserve", soc_ceiling(rate))),
        # Self-consumption as found already covers the house.
        ACTION_DISCHARGE_HOUSE: lambda _rate, _w: (),
    },
    enter_order=("grid_charging", "reserve"),
    # The reserve first: it stops a forced charge at once.
    restore_order=("reserve", "grid_charging"),
    peak_first={ACTION_GRID_CHARGE: ("reserve",)},
    slow=True,
    min_change_s=900.0,
    rearm_s=900.0,
    fixed_charge=True,
    fixed_charge_kw=5.0,
    write_caps={"setting": TESLA_WRITES_PER_DAY},
    # The hardware test: three modes after one another, each after a 15-minute re-arm and two cloud read-backs of up
    # to 7 minutes (`verify.quick_for`); about 55 minutes when the cloud is at its slowest, with room for the looks.
    test_total_s=75 * 60.0,
    soc_floor_margin_pct=2.0,
    hold_grid_guard=True,
    hold_charges_without=("grid_charging",),
    # The reserve one block ahead of the SoC, set again each block by SpotNav: a SoC bound, never a fuse argument.
    expiry_by_action={ACTION_GRID_CHARGE: Expiry("soc_bound", "spotnav", TESLA_CHARGE_BLOCK_S, ("reserve",))},
    # A restore given up is tried hourly, not every 15 minutes (each is a billed command).
    restore_retry_s=3600.0,
    note="Cloud, slow: planned actions only. The charge power is the Powerwall's own (about 1.7 kW each in field"
    " reports): a charge that does not fit under the fuse is refused, not lowered.",
)

_FA_CMD = Role(
    "command", "service", _key("last_cmd"), scale=1000.0, step=500.0, restored=False,
    services={"charge": "ferroamp.charge", "discharge": "ferroamp.discharge", "auto": "ferroamp.autocharge"},
    # `target` is the EnergyHub's device id; a request has no duration (nothing renews it): no lease.
    device_field="target",
    normalize=ferroamp_last_cmd, write_class="setpoint",
    # The state is the request as published; only the hub's ack for that transaction shows it was taken.
    answer=Answer("transId", "status", frozenset({"ack"})), readback="answer",
)


def _fa(kind: str) -> Callable[[float, int], tuple[Want, ...]]:
    def build(rate_kw: float, _watchdog_s: int) -> tuple[Want, ...]:
        return (Want("command", f"{kind}:{int(native(_FA_CMD, rate_kw))}"),)

    return build


#: Ferroamp's holds, in the order the verification tries them: a request of 0 W either way (neither is documented).
FERROAMP_HOLDS: Final = (
    HoldVariant("charge_zero", lambda _r, _w: (Want("command", "charge:0"),), ("command",), "charge at 0 W", idle=True),
    HoldVariant(
        "discharge_zero", lambda _r, _w: (Want("command", "discharge:0"),), ("command",), "discharge at 0 W", idle=True
    ),
)

FERROAMP_CONTROL: Final = BrandControl(
    brand="ferroamp",
    # Not field-tested on a plant of the brand: writes only after a passed hardware test (a plant whose control
    # already ran keeps it, `GATED_AFTER_RUNNING`).
    hardware_test_first=True,
    adapter=FERROAMP,
    roles=(_FA_CMD,),
    states={
        ACTION_HOLD: FERROAMP_HOLDS[0].build,
        ACTION_GRID_CHARGE: _fa("charge"),
        # The hub's own strategy covers the house from the battery: `discharge` at a power could export.
        ACTION_DISCHARGE_HOUSE: lambda _r, _w: (Want("command", "auto"),),
        ACTION_EXPORT_DISCHARGE: _fa("discharge"),
    },
    enter_order=("command",),
    restore_order=("command",),
    # Someone else's request left standing (Ferroamp's own app, an automation) is not overwritten.
    requires=(("command", frozenset({"auto"}), "ferroamp_command_running"),),
    peak_first={ACTION_HOLD: ("command",), ACTION_GRID_CHARGE: ("command",)},
    # The strategy set in EnergyCloud (peak shaving, FerroAI, a grid service) is not shown in Home Assistant.
    strategy_unread=True,
    # The cap counts the whole charge on one phase (the default): the API takes the system's total and nobody
    # documents how the hub spreads it, nor whether ACE acts on it. Raise it only once a hub has shown the split
    # (the adapter reads the inverter's power per phase).
    grid_charge_phases=1,
    hold_variants=FERROAMP_HOLDS,
    variant=FERROAMP_HOLDS[0].name,
    # The spec says nothing of how often a request may be sent, nor of a timeout: the moderate cap.
    write_caps={"setpoint": SETPOINT_WRITES_UNSTATED},
    note="Untested. A request stays until another one or `auto`; one request at a time, a second is nak'd.",
)

# --- FoxESS (limits only; see the module) --------------------------------------------------------------------------

# The battery's own current limits (A, holding registers written at once; read back as polled). Restored as found.
_FX_DISCHARGE = Role("discharge_current", "number", _key("max_discharge_current", "number"), step=0.1)
_FX_CHARGE = Role("charge_current", "number", _key("max_charge_current", "number"), step=0.1)

FOXESS_CONTROL: Final = BrandControl(
    brand="foxess",
    adapter=FOXESS,
    roles=(_FX_DISCHARGE, _FX_CHARGE),
    states={
        # The battery covers nothing; a sun surplus is still stored (not an idle hold).
        ACTION_HOLD: lambda _rate, _w: (Want("discharge_current", 0.0),),
        # It covers the house and stores nothing (a sun surplus is exported).
        ACTION_DISCHARGE_HOUSE: lambda _rate, _w: (Want("charge_current", 0.0),),
        # No grid charge and no sale: the integration's remote control falls back to Back-up (see the module).
    },
    enter_order=("discharge_current", "charge_current"),
    restore_order=("discharge_current", "charge_current"),
    # A hold's peak cover gives the discharge current back.
    peak_first={ACTION_HOLD: ("discharge_current",)},
    # Lower limits never add import, so nothing needs to end by itself (`NO_EXPIRY`, the default).
    hardware_test_first=True,
    note="Limits only: the battery's max discharge current at 0 to hold, its max charge current at 0 to cover the"
    " house without storing. No grid charge or sale yet. Never run on a device; writes only after a passed hardware"
    " test.",
)

# --- EcoFlow PowerOcean (see the module) ---------------------------------------------------------------------------

#: The option of `ef_powerocean_tcpmodbus`'s `battery_mode` select (`ControlFeature`, `powerocean/models.py`) each
#: state writes: the inverter's own self-consumption (control method 0), and method 3 with the battery's setpoint at
#: 1 W (a hold: 0 W is "no limit" to the inverter), at plus or at minus the mode's power.
_EF_AUTOMATIC: Final = "automatic"
_EF_HOLD: Final = "hold_battery"
_EF_CHARGE: Final = "charge_battery"
_EF_DISCHARGE: Final = "discharge_battery"
#: The integration's heartbeat window (s): the inverter obeys the commands it stored for this long after the last
#: heartbeat, which the integration writes every 20 s while Modbus Control is on (`powerocean/const.py`).
ECOFLOW_HEARTBEAT_WINDOW_S: Final = 60.0

# Modbus Control: the integration's own switch, which starts and stops its heartbeat (persisted by the integration;
# its state is the integration's flag, so its read-back is that flag, and the battery's power judges the effect).
_EF_SWITCH = Role("modbus_control", "switch", _key("modbus_control", "switch"), restored=False, readback="echo")
# The mode and the two powers are available only while the heartbeat holds control: idle (unavailable) otherwise.
# `ecoflow_energy`'s `<sn>_local_battery_mode` ends in the same key and is not this select.
_EF_MODE = Role(
    "battery_mode", "select", _key("battery_mode", "select", reject=r"local_battery_mode$"), restored=False,
    idle_when=frozenset({"unavailable"}), readback="echo",
)
# The power of the integration's own Charge Battery and Discharge Battery modes (W, step 100), kept by it and sent
# only while that mode is selected. Never handed back: unavailable before the switch is on, so there is no value to
# snapshot, and harmless in every other mode.
_EF_CHARGE_POWER = Role(
    "charge_power", "number", _key("charge_battery_power", "number"), scale=1000.0, step=100.0, restored=False,
    idle_when=frozenset({"unavailable"}), write_class="setpoint", readback="echo",
)
_EF_DISCHARGE_POWER = Role(
    "discharge_power", "number", _key("discharge_battery_power", "number"), scale=1000.0, step=100.0,
    restored=False, idle_when=frozenset({"unavailable"}), write_class="setpoint", readback="echo",
)


def _ef(mode: str, power: str | None = None) -> Callable[[float, int], tuple[Want, ...]]:
    """Modbus Control on, the mode's power (W) where it has one, then the mode. A power that rounds to 0 W is a hold
    instead: the inverter reads a setpoint of 0 as no limit at all (the integration does the same)."""

    def build(rate_kw: float, _watchdog_s: int) -> tuple[Want, ...]:
        if power is None:
            return (Want("modbus_control", True), Want("battery_mode", mode))
        size = native(_EF_CHARGE_POWER, rate_kw)
        if size <= 0:
            return (Want("modbus_control", True), Want("battery_mode", _EF_HOLD))
        return (Want("modbus_control", True), Want(power, size), Want("battery_mode", mode))

    return build


#: EcoFlow's one hold: Hold Battery, the battery idle (it stores no sun either; at 100 % it curtails the panels,
#: MaxGrmm #83). Its peak cover writes Automatic, where the inverter covers the house itself.
ECOFLOW_HOLDS: Final = (
    HoldVariant("hold_battery", _ef(_EF_HOLD), ("battery_mode",), "Hold Battery (setpoint 1 W)", idle=True),
)

ECOFLOW_CONTROL: Final = BrandControl(
    brand="ecoflow",
    adapter=ECOFLOW,
    roles=(_EF_SWITCH, _EF_CHARGE_POWER, _EF_DISCHARGE_POWER, _EF_MODE),
    states={
        ACTION_HOLD: ECOFLOW_HOLDS[0].build,
        ACTION_GRID_CHARGE: _ef(_EF_CHARGE, "charge_power"),
        # The inverter's own self-consumption under Modbus Control: it covers the house (and stores a sun surplus).
        ACTION_DISCHARGE_HOUSE: _ef(_EF_AUTOMATIC),
        ACTION_EXPORT_DISCHARGE: _ef(_EF_DISCHARGE, "discharge_power"),
    },
    # The switch first (the rest is unavailable until its heartbeat holds control), the power before the mode that
    # sends it. Back: Automatic first, which the live heartbeat sends at once (method 0), then the switch off, which
    # only stops the heartbeat: the inverter returns to the app's settings within its window.
    enter_order=("modbus_control", "charge_power", "discharge_power", "battery_mode"),
    restore_order=("battery_mode", "modbus_control"),
    own_states={"Modbus control": frozenset({"on"})},
    requires=(("modbus_control", frozenset({"off"}), "modbus_control_already_on"),),
    peak_first={ACTION_HOLD: ECOFLOW_HOLDS[0].peak_first, ACTION_GRID_CHARGE: ("battery_mode",)},
    # The inverter's own heartbeat dead-man, renewed by the integration (not by SpotNav): it ends the command when Home
    # Assistant stops, never when SpotNav alone stalls, so it never lifts a cap (`gate.expiry_lifts`). Stated by the
    # integration and measured by another integration's author on one HJ31 (30-65 s); not yet seen by SpotNav. The
    # switch off is the silence its hardware test times it with.
    expiry=Expiry(
        "device_deadman", "integration", ECOFLOW_HEARTBEAT_WINDOW_S, integration="ef_powerocean_tcpmodbus",
        note="heartbeat 40608, written by the integration every 20 s while Modbus Control is on",
        silence=(("modbus_control", False),),
    ),
    # A three-phase PowerOcean balances its phases itself, but how it spreads a grid charge is not stated anywhere:
    # the HA-down cap takes the whole charge on one phase until a plant has shown the split.
    grid_charge_phases=1,
    hold_variants=ECOFLOW_HOLDS,
    variant=ECOFLOW_HOLDS[0].name,
    # The controls are the HACS integration `ef_powerocean_tcpmodbus`'s; `ecoflow_energy` (read only here) has its own.
    hacs_roles=("charge_power", "discharge_power", "battery_mode"),
    hacs_repo="MaxGrmm/EF-PowerOcean-TcpModbus",
    # The protocol (EcoFlow Open Modbus V1.0, a trial version) says nothing of how often the setpoint may be written.
    write_caps={"setpoint": SETPOINT_WRITES_UNSTATED},
    hardware_test_first=True,
    note="Modbus Control through ef_powerocean_tcpmodbus: the EcoFlow app is locked while it is on and for about a"
    " minute after. Untested on a plant: runs only after the site's hardware test passed on its firmware.",
)

# --- Marstek Venus (marstek_modbus, see the module) ------------------------------------------------------------------

#: The Venus E's largest charge power (kW; the entity's own maximum, 2500 W): a rate above it is written as it, never
#: refused by the entity. The Venus A's is 1500 W: a write above it is refused and handed back.
MARSTEK_MAX_KW: Final = 2.5
_MS_RS485 = Role("rs485_control", "switch", _key("rs485_control_mode", "switch"))
_MS_FORCE = Role("force_mode", "select", _key("force_mode", "select"), write_class="setpoint")
# 42020 acts only in a force charge with RS485 control on, and is written only with it on: never written back (a
# change of action turns RS485 control off before it would come in turn), the next charge writes it before the mode.
_MS_CHARGE = Role(
    "charge_power", "number", _key("set_charge_power", "number"), scale=1000.0, step=50.0, restored=False,
    write_class="setpoint",
)
_MS_MAX_DISCHARGE = Role(
    "max_discharge_power", "number", _key("max_discharge_power", "number"), scale=1000.0, step=50.0
)


def _ms_charge(rate_kw: float, _watchdog_s: int) -> tuple[Want, ...]:
    return (
        Want("rs485_control", True), Want("charge_power", native(_MS_CHARGE, min(rate_kw, MARSTEK_MAX_KW))),
        Want("force_mode", "charge"),
    )


MARSTEK_CONTROL: Final = BrandControl(
    brand="marstek",
    adapter=MARSTEK,
    roles=(_MS_MAX_DISCHARGE, _MS_RS485, _MS_CHARGE, _MS_FORCE),
    states={
        # A limit safe to keep: the battery's own self-consumption, discharging nothing (the sun is still stored).
        ACTION_HOLD: lambda _rate, _w: (Want("max_discharge_power", 0.0),),
        ACTION_GRID_CHARGE: _ms_charge,
        # The battery's own self-consumption covers the house.
        ACTION_DISCHARGE_HOUSE: lambda _rate, _w: (),
    },
    # RS485 control first (42xxx act only with it on), the power before the mode, so the mode never runs at a power
    # left there. Back: the force mode to standby first (it stops the charge), then RS485 control off.
    enter_order=("max_discharge_power", "rs485_control", "charge_power", "force_mode"),
    restore_order=("force_mode", "rs485_control", "max_discharge_power"),
    # RS485 control that SpotNav turned on is not someone else's while it runs.
    own_states={"RS485 control": frozenset({"on"})},
    requires=(("force_mode", frozenset({"standby"}), "force_mode_left_standing"),),
    peak_first={ACTION_HOLD: ("max_discharge_power",), ACTION_GRID_CHARGE: ("force_mode",)},
    # A single-phase battery: the whole charge on its one phase.
    grid_charge_phases=1,
    # No dead-man on any Modbus route (the module): a force charge stays until changed.
    expiry=NO_EXPIRY,
    hacs_roles=("max_discharge_power", "rs485_control", "charge_power", "force_mode"),
    hacs_repo="ViperRNMC/marstek_venus_modbus",
    # Nothing says whether the force registers are kept in flash: the moderate cap.
    write_caps={"setpoint": SETPOINT_WRITES_UNSTATED},
    # Never run on a device: nothing is written until a person's hardware test passed on the plant's firmware.
    hardware_test_first=True,
    # A Venus A's Set Charge Power stops at 1500 W (and refuses a write above it), a Venus E's at 2500 W.
    charge_power_bound="charge_power",
    note="Needs the Modbus integration (ViperRNMC/marstek_venus_modbus). Hold: the maximum discharge power at 0. Grid"
    " charge: RS485 control with a force charge, which the battery keeps if Home Assistant stops (no dead-man), so it"
    " runs under the HA-down cap. Never run on a device; writes only after a passed hardware test.",
)

# --- Solis (solis_modbus, see the module) ----------------------------------------------------------------------------

#: The RC Timeout SpotNav sends with each force (s; the register is in whole minutes, 1-30: the firmware's default 5)
#: and how often it renews the force (two renewals may be lost before it lapses): the inverter's own dead-man, stated
#: by the integration and timed by the hardware test, never lifting a cap (longer than `gate.SILENCE_FALLBACK_MAX_S`).
SOLIS_RC_WINDOW_S: Final = 300.0
SOLIS_RC_RENEW_S: Final = 120
#: The renewals a day (`BrandControl.renewal_cap`): a day of force at one every two minutes is 720.
SOLIS_RENEWALS_A_DAY: Final = 800
#: What the force reads when none runs (both switches off), and the services that write it: 43135 with the power and
#: the timeout in one batch (`__init__.py` `_force_battery`, issue #352's order), and 43135 = 0 alone.
SOLIS_RC_STOP: Final = "stop"
_SOLIS_SERVICES: Final = {
    "charge": "solis_modbus.solis_force_battery_charge",
    "discharge": "solis_modbus.solis_force_battery_discharge",
    SOLIS_RC_STOP: "solis_modbus.solis_stop_force_charge_discharge",
}
#: The force powers' register readings (the sensors of 43136 and 43129: updated only by a poll or an executed write,
#: never by the number's own optimistic echo).
_SOLIS_CHARGE_READ: Final = _key("solis_modbus_inverter_rc_force_charge_power", "sensor")
_SOLIS_DISCHARGE_READ: Final = _key("solis_modbus_inverter_rc_force_discharge_power", "sensor")


def _solis_force(charge: str | None, parts: tuple[str | None, ...]) -> str | None:
    """The force as compared, from the switch "RC Force Battery Charge" (the role's own entity), the switch "RC Force
    Battery Discharge" and the two power readings: `charge:<W>`, `discharge:<W>` or `stop`; `None` when one of them
    reads nothing."""
    discharge, charge_w, discharge_w = (*parts, None, None, None)[:3]
    states = [state if state is None else state.strip().lower() for state in (charge, discharge)]
    if any(state not in ("on", "off") for state in states):
        return None
    if states == ["off", "off"]:
        return SOLIS_RC_STOP
    text = charge_w if states[0] == "on" else discharge_w
    try:
        watts = float(text) if text is not None else math.nan
    except ValueError:
        return None
    if not math.isfinite(watts) or states == ["on", "on"]:
        return None
    return f"{'charge' if states[0] == 'on' else 'discharge'}:{int(round(watts))}"


# The force, written through the integration's services with its power and the timeout in one call (no cached
# companion is ever re-sent) and read back from the register's two switches and the power readings.
_SOLIS_FORCE = Role(
    "rc_force", "service", _key("43135_1", "switch"), services=_SOLIS_SERVICES, device_field="", lease=True,
    power_field="power_watts", duration_field="duration_minutes",
    parts=(_key("43135_2", "switch"), _SOLIS_CHARGE_READ, _SOLIS_DISCHARGE_READ), compose=_solis_force,
    write_class="setpoint", step=10.0, scale=1000.0,
)
def _solis_register(_own: str | None, parts: tuple[str | None, ...]) -> str | None:
    """A number read back by its register (the sensor of the same register), never by its own optimistic echo."""
    return parts[0] if parts else None


# The force powers and the timeout, written before the force (with no force running, nothing re-sends them) and each
# read back by its register: the force then runs at a power and a timeout SpotNav saw the inverter take, even if its
# own call reaches the inverter only in part. The powers are never set back to the value found: a hand-back leaves
# them at 0 (the integration's select re-sends them from its cache whenever someone sets a force by hand).
_SOLIS_CHARGE = Role(
    "charge_power", "number", _key("solis_modbus_inverter_rc_force_charge_power", "number"), scale=1000.0,
    step=10.0, restored=False, write_class="setpoint", parts=(_SOLIS_CHARGE_READ,), compose=_solis_register,
)
_SOLIS_DISCHARGE = Role(
    "discharge_power", "number", _key("solis_modbus_inverter_rc_force_discharge_power", "number"), scale=1000.0,
    step=10.0, restored=False, write_class="setpoint", parts=(_SOLIS_DISCHARGE_READ,), compose=_solis_register,
)
# The RC Timeout: set before the force and sent with it again; handed back as found once no force runs.
_SOLIS_TIMEOUT = Role(
    "rc_timeout", "number", _key("solis_modbus_inverter_rc_timeout", "number"), step=1.0, write_class="setpoint",
    parts=(_key("solis_modbus_inverter_rc_timeout", "sensor"),), compose=_solis_register,
)
# "Allow Grid to Charge the Battery" (43110 bit 5): read for a grid charge, never written.
_SOLIS_GRID_CHARGE = Role("grid_charge_allowed", "switch", _key("43110_5", "switch"), restored=False)


def _solis(kind: str) -> Callable[[float, int], tuple[Want, ...]]:
    """The force's power and the timeout first, then the force of `kind` at that power (a charge at 0 W for a hold)."""

    def build(rate_kw: float, _watchdog_s: int) -> tuple[Want, ...]:
        watts = int(native(_SOLIS_FORCE, rate_kw))
        power = "charge_power" if kind == "charge" else "discharge_power"
        return (
            Want(power, float(watts)), Want("rc_timeout", SOLIS_RC_WINDOW_S / 60), Want("rc_force", f"{kind}:{watts}"),
        )

    return build


_SOLIS_PHASES_IN_TYPE: Final = re.compile(r"(?<![0-9a-z])([13])-phase(?![0-9a-z])")


def _solis_phases(state: str) -> int | None:
    """The phases in the inverter's own type as `solis_modbus` words it ("S6 3-Phase HV Hybrid (12-20kW)(Protocol
    3)", "1-Phase LV Hybrid Inverter(Protocol 2)"); `None` for a text that names none ("Unknown Model", "No
    definition", "5-15kWh All-in-One Hybrid") or more than one."""
    found = set(_SOLIS_PHASES_IN_TYPE.findall(state.lower()))
    return int(found.pop()) if len(found) == 1 else None


SOLIS_CONTROL: Final = BrandControl(
    brand="solis",
    adapter=SOLIS,
    roles=(_SOLIS_FORCE, _SOLIS_TIMEOUT, _SOLIS_CHARGE, _SOLIS_DISCHARGE, _SOLIS_GRID_CHARGE),
    states={
        # RC force charge at 0 W: the battery covers nothing (seen 68 min on a tester's plant); a sun surplus is
        # still stored.
        ACTION_HOLD: lambda _rate, watchdog_s: _solis("charge")(0.0, watchdog_s),
        ACTION_GRID_CHARGE: _solis("charge"),
        # The inverter's own Self-Use covers the house: everything as found (no force).
        ACTION_DISCHARGE_HOUSE: lambda _rate, _w: (),
        # A battery power: the house is covered first, the rest sold. Never at 0 W (what that does is not known).
        ACTION_EXPORT_DISCHARGE: _solis("discharge"),
    },
    # The power and the timeout first, each read back by its register while no force runs, then the force's call
    # (which sends them again with 43135). Back: the force stopped first (43135 = 0 alone), then both force powers at 0
    # and the timeout as found.
    enter_order=("charge_power", "discharge_power", "rc_timeout", "rc_force"),
    restore_order=("rc_force", "charge_power", "discharge_power", "rc_timeout"),
    # SpotNav's own force is not someone else's while it runs (the adapter's mode rules read 43135's switches).
    own_states={"remote force charge": frozenset({"on"}), "remote force discharge": frozenset({"on"})},
    # A force standing when SpotNav would start is another controller's (an outside EMS, SolisCloud).
    requires=(("rc_force", frozenset({SOLIS_RC_STOP}), "rc_force_running"),),
    action_requires=((ACTION_GRID_CHARGE, "grid_charge_allowed", frozenset({"off"}), "grid_charge_not_allowed"),),
    # A hold's peak cover and a charge's stop: the force stopped, the inverter's own Self-Use.
    peak_first={ACTION_HOLD: ("rc_force",), ACTION_GRID_CHARGE: ("rc_force",)},
    # The powers and the timeout act in a force: never written while another system's force runs (`command_role`), and
    # on a change of action a timeout that falls is written before the force's change, one that rises after it.
    mode_limits=("charge_power", "discharge_power", "rc_timeout"),
    command_role="rc_force",
    # The RC Timeout: the force reverts by itself (both switches off) that long after the last call. Stated by the
    # integration, timed by the hardware test; longer than any window that lifts a cap.
    expiry=Expiry(
        "device_deadman", "spotnav", SOLIS_RC_WINDOW_S, ("rc_force",), renew_s=SOLIS_RC_RENEW_S,
        note="RC Timeout 43282 at 5 minutes, sent with each force call; renewed every 2 minutes",
        fallback_values=(("rc_force", SOLIS_RC_STOP),),
    ),
    renewal_cap=SOLIS_RENEWALS_A_DAY,
    deadman_must_pass=True,
    # Below its Force Charge SOC (43018) the inverter charges from the grid by itself at its Force-charge Power
    # Limitation (43027, W): the research's reading of the protocol, not seen on a plant.
    autonomous_import=AutonomousImport(
        entity=_key("solis_modbus_inverter_battery_force_charge_limit", "number", "sensor"), scale=0.001,
        trigger="below_reserve", reserve=_key("solis_modbus_inverter_force_charge_soc", "number", "sensor"),
        why="the inverter's own force charge below its Force Charge SOC (unverified)", near_reserve_only=True,
    ),
    # A three-phase hybrid is counted as balanced over its phases, charge and discharge alike (owner decision
    # 2026-11-26; see the module); three-phase only when the inverter's own type says so, else all on one phase.
    grid_charge_phases=1,
    output_type=_key("solis_modbus_inverter_model_definition"),
    output_phases=_solis_phases,
    balanced_output=True,
    write_caps={"setpoint": SETPOINT_WRITES_UNSTATED},
    bms_charge_limit=True,
    hardware_test_first=True,
    recipe_rev=2,
    note="The inverter's RC force through solis_modbus's services (Pho3niX90). Hold: force charge at 0 W; grid"
    " charge: force charge at the rate, within the BMS's charge limit; sale: force discharge. Each call sends its"
    " power and a 5-minute RC Timeout; SpotNav renews every 2 minutes (stated, timed by the hardware test). Never run"
    " on a plant; writes only after a passed hardware test.",
)

# --- Kostal Plenticore (kostal_modbus_control, see the module) -------------------------------------------------------

#: What the integration's `battery_mgmt_mode` reads (register 1080 = 2, `const.py` `BATTERY_MGMT_MODE_MAP`) when the
#: installer set "External via protocol (Modbus TCP)": the only mode in which the inverter takes a write.
KOSTAL_MGMT_MODBUS: Final = "External battery management via Modbus protocol"
#: The inverter's external-control timeout (s): an installer's web setting (30 and 60 s seen), not readable. The
#: longer one: a plant set to 30 s falls back sooner. Renewed by the integration's loops at half its own setting.
KOSTAL_TIMEOUT_S: Final = 60.0

# Set Charge Rate: the integration's own value (W, step 100), which Charge Start's loop sends as the setpoint at every
# tick; its state is that value, so the echo is what the inverter is sent. A value at or above its maximum latches
# "follow the maximum" (`follows_max` true): such a write is not taken. Written back last as found (the integration
# keeps it over a restart, and the person's own Charge Start runs at it): a rate that followed the maximum by writing
# the maximum again.
_KO_RATE = Role(
    "charge_rate", "number", _key("charge_rate", "number"), scale=1000.0, step=100.0,
    write_class="setpoint", readback="echo", follows_max="follows_max",
)
# The two switches SpotNav turns on. Their state is the integration's flag (on at once, its loop after any settling
# wait), so the battery's power judges the effect.
_KO_CHARGE = Role("charge_start", "switch", _key("charge_start", "switch"), write_class="setpoint", readback="echo")
_KO_HOLD = Role("block_discharge", "switch", _key("block_discharge", "switch"), write_class="setpoint", readback="echo")
# Register 1080: read before anything is written, never written (an installer's setting).
_KO_MGMT = Role("management_mode", "sensor", _key("battery_mgmt_mode"), restored=False)


def _ko_charge(rate_kw: float, _watchdog_s: int) -> tuple[Want, ...]:
    """The rate first, read back, then Charge Start, whose loop sends it."""
    return (Want("charge_rate", native(_KO_RATE, rate_kw)), Want("charge_start", True))


KOSTAL_CONTROL: Final = BrandControl(
    brand="kostal",
    adapter=KOSTAL,
    roles=(_KO_MGMT, _KO_HOLD, _KO_RATE, _KO_CHARGE),
    states={
        # Block Discharge: the battery covers nothing; the sun is still stored.
        ACTION_HOLD: lambda _rate, _w: (Want("block_discharge", True),),
        ACTION_GRID_CHARGE: _ko_charge,
        # The inverter's own control covers the house: no switch on.
        ACTION_DISCHARGE_HOUSE: lambda _rate, _w: (),
    },
    # A hold's switch off before the rate and Charge Start (the integration would turn it off itself, and SpotNav would
    # read its own switch changed); a charge's Charge Start off first on every change and hand-back (`peak_first`).
    enter_order=("block_discharge", "charge_rate", "charge_start"),
    restore_order=("charge_start", "block_discharge", "charge_rate"),
    # SpotNav's own switches are not someone else's while they run (the adapter's mode rules read every switch).
    own_states={"Charge Start": frozenset({"on"}), "Block Discharge": frozenset({"on"})},
    requires=(("management_mode", frozenset({KOSTAL_MGMT_MODBUS}), "external_control_off"),),
    peak_first={ACTION_HOLD: ("block_discharge",), ACTION_GRID_CHARGE: ("charge_start",)},
    # The inverter's own timeout, renewed by the integration's loops (not by SpotNav): it ends a command when Home
    # Assistant stops, never when SpotNav alone stalls, so it never lifts a cap (`gate.expiry_lifts`). Stated by the
    # integration, evcc and owners; no write of SpotNav's stops the loops and leaves the command standing (turning a
    # switch off writes the stop at once), so it has no silence and is not timed by the hardware test.
    expiry=Expiry(
        "device_deadman", "integration", KOSTAL_TIMEOUT_S, integration="kostal_modbus_control",
        note="the inverter's external-control timeout (installer setting, 30 or 60 s seen), written every half of it"
        " by the integration's switch loops",
    ),
    # A three-phase hybrid; how it spreads a grid charge over the phases is not stated: all of it on one.
    grid_charge_phases=1,
    # The inverter's own strategy (smart battery control, time control, core's select) is not in this integration's
    # entry: the person acknowledges that SpotNav's commands override whatever runs.
    strategy_unread=True,
    # A minimum SoC hold (the integration's other option) may let the inverter reach it from the grid (unverified):
    # a hold that charges from the grid did not take.
    hold_grid_guard=True,
    hacs_roles=("management_mode", "block_discharge", "charge_rate", "charge_start"),
    hacs_repo="mbuhansen/kostal_plenticore_battery_control",
    # Kostal's interface description says nothing of flash; the integration and evcc call the registers volatile.
    write_caps={"setpoint": SETPOINT_WRITES_UNSTATED},
    charge_power_bound="charge_rate",
    charge_power_bound_exclusive=True,
    hardware_test_first=True,
    note="Needs kostal_modbus_control (mbuhansen) and the installer's External via protocol (Modbus TCP). Hold: Block"
    " Discharge; grid charge: Set Charge Rate and Charge Start. The integration renews them; the inverter falls back"
    " by itself within its timeout when Home Assistant stops, so a grid charge still runs under the HA-down cap. Never"
    " run on a plant; writes only after a passed hardware test.",
)

# --- Fronius Gen24 (core fronius over Modbus, see the module) ------------------------------------------------------

#: The revert period core `fronius` writes with every limit while its option "Revert the power and battery limits if
#: Home Assistant stops" is on (s; `const.AUTO_REVERT_SECONDS`), and resends the active limits within (every 15 min).
FRONIUS_REVERT_S: Final = 3600.0

# The two limits (% of WChaMax, -100..100, step 1) and their switches (StorCtl_Mod's two bits), each read back by core's
# refresh right after the write. A limit found below 0 (someone's forcing value left behind its switch) is handed back
# as 100, no limit: never a force to leave for the next one who turns its switch on.
_FR_CHARGE = Role(
    "charge_limit", "number", _fronius("battery_charge_power_limit", "number"), step=1.0, write_class="setpoint",
    restored_below=(0.0, 100.0),
)
_FR_DISCHARGE = Role(
    "discharge_limit", "number", _fronius("battery_discharge_power_limit", "number"), step=1.0,
    write_class="setpoint", restored_below=(0.0, 100.0),
)
_FR_CHARGE_ON = Role(
    "charge_limit_on", "switch", _fronius("battery_charge_power_limit_enabled", "switch"), write_class="setpoint"
)
_FR_DISCHARGE_ON = Role(
    "discharge_limit_on", "switch", _fronius("battery_discharge_power_limit_enabled", "switch"),
    write_class="setpoint",
)
# Read for a grid charge, never written (ChaGriSet keeps its value whatever happens to Home Assistant).
_FR_GRID_CHARGING = Role("grid_charging", "switch", _fronius("battery_grid_charging", "switch"), restored=False)


def _fr_hold(_rate_kw: float, _watchdog_s: int) -> tuple[Want, ...]:
    return (Want("discharge_limit", 0.0), Want("discharge_limit_on", True))


def _fr_charge(rate_kw: float, watchdog_s: int) -> tuple[Want, ...]:
    """The discharge limit at minus the rate's share of WChaMax: a forced charge at least that fast; and the charge
    limit at the same share: at most that fast, so neither the sun nor the grid takes the charge above the rate
    (`fronius_control_2026-11-23.md`: a charge cap of +p is what holds the total). The pair sums to 0, which core
    accepts. A rate of nothing is a hold."""
    if rate_kw <= 0:
        return _fr_hold(rate_kw, watchdog_s)
    return (
        Want("charge_limit", pct_of_rated(rate_kw)), Want("charge_limit_on", True),
        Want("discharge_limit", pct_of_rated(-rate_kw)), Want("discharge_limit_on", True),
    )


FRONIUS_CONTROL: Final = BrandControl(
    brand="fronius",
    adapter=FRONIUS,
    roles=(_FR_CHARGE, _FR_DISCHARGE, _FR_CHARGE_ON, _FR_DISCHARGE_ON, _FR_GRID_CHARGING),
    states={
        # The battery covers nothing; a sun surplus is still stored (not an idle hold).
        ACTION_HOLD: _fr_hold,
        ACTION_GRID_CHARGE: _fr_charge,
        # It covers the house and stores nothing (a sun surplus is exported).
        ACTION_DISCHARGE_HOUSE: lambda _rate, _w: (Want("charge_limit", 0.0), Want("charge_limit_on", True)),
        # No sale yet: a negative charge limit has not been seen on a plant (see the module).
    },
    # The values first (a value written while its switch is off does nothing), the charge limit's switch before the
    # discharge limit's: from a discharge to the house into a hold or a charge it goes off before the other comes on.
    # Back: both switches off first (the inverter's own priority list again), then the values as found.
    enter_order=("charge_limit", "discharge_limit", "charge_limit_on", "discharge_limit_on"),
    restore_order=("discharge_limit_on", "charge_limit_on", "discharge_limit", "charge_limit"),
    # SpotNav's own limits are not someone else's while they run (the adapter's mode rules read the two switches).
    own_states={"charge limit": frozenset({"on"}), "discharge limit": frozenset({"on"})},
    # A limit in force when SpotNav would start is another controller's (evcc, a script, a person).
    requires=(
        ("charge_limit_on", frozenset({"off"}), "charge_limit_in_force"),
        ("discharge_limit_on", frozenset({"off"}), "discharge_limit_in_force"),
    ),
    action_requires=((ACTION_GRID_CHARGE, "grid_charging", frozenset({"off"}), "grid_charging_off"),),
    # A hold's peak cover and a charge's stop: the discharge limit's switch off, the battery covers the house again.
    peak_first={ACTION_HOLD: ("discharge_limit_on",), ACTION_GRID_CHARGE: ("discharge_limit_on",)},
    # The inverter's revert timer, written and renewed by core only with its option on: a dead-man the integration
    # keeps alive (never SpotNav, so a stalled SpotNav's limit stands while Home Assistant runs), never lifting a cap.
    expiry=Expiry(
        "device_deadman", "integration", FRONIUS_REVERT_S, integration="fronius",
        note="InOutWRte_RvrtTms at 3600 s, resent every 15 minutes by core, only with its revert option on (not read)",
    ),
    # Web-UI schedules, the Energy Cost Assistant and dynamic power reduction cannot be read from Home Assistant.
    strategy_unread=True,
    # How a three-phase Gen24 spreads a forced charge over its phases is not stated: the whole charge on one phase.
    grid_charge_phases=1,
    write_caps={"setpoint": SETPOINT_WRITES_UNSTATED},
    rating_read=True,
    # A grid charge holds both limits on at +p and -p: a change of rate writes the one that rises first, so the pair
    # never sums below 0 on the way (core refuses that pair).
    rising_first=("charge_limit", "discharge_limit"),
    hardware_test_first=True,
    note="Modbus limits through core fronius (HA 2026.11 or later). Hold: the discharge limit at 0; grid charge: the"
    " discharge limit below 0 (a forced charge at the rate's share of WChaMax); house: the charge limit at 0. No"
    " sale. Core's revert option is the only dead-man and never lifts a cap. Never run on a device; writes only"
    " after a passed hardware test.",
)

# --- Deye / Sunsynk / Sol-Ark (limits only; see the module) --------------------------------------------------------

#: The battery's own current limits (A), as `solarman`'s Deye profiles name them (`Battery Max Charging Current`,
#: `... Discharging Current`: `<entry id>_battery_max_charging_current_number`) and kellerza's add-on
#: (`Battery Max Charge current`: `<serial>_battery_max_charge_current`, the add-on's sensor groups "advanced" or
#: "battery"). Whole amps (both round to an integer register). Restored as found. `solarman` shows a value once the
#: logger acknowledged it, kellerza's add-on reads its settings every 5 s: the ledger's own read-back wait.
_DY_DISCHARGE = Role(
    "discharge_current", "number",
    _any_key("battery_max_discharging_current_number", "battery_max_discharge_current", domains=("number",)),
    step=1.0,
)
_DY_CHARGE = Role(
    "charge_current", "number",
    _any_key("battery_max_charging_current_number", "battery_max_charge_current", domains=("number",)),
    step=1.0,
)
#: Register writes a day, every one counted (restores and peak covers too, `BrandControl.count_restores`): half a
#: setting's, as both limits are inverter settings that nobody says are kept out of flash, and the community worries
#: about wear (kellerza #706).
DEYE_SETTING_WRITES: Final = 48

DEYE_CONTROL: Final = BrandControl(
    brand="deye",
    adapter=DEYE,
    roles=(_DY_DISCHARGE, _DY_CHARGE),
    states={
        # The battery covers nothing; a sun surplus is still stored (evcc's `holddischarge`; not an idle hold).
        ACTION_HOLD: lambda _rate, _w: (Want("discharge_current", 0.0),),
        # It covers the house and stores nothing (a sun surplus is exported, or curtailed in a zero-export work mode).
        ACTION_DISCHARGE_HOUSE: lambda _rate, _w: (Want("charge_current", 0.0),),
        # No grid charge and no sale: only by rewriting the owner's time-of-use programmes (see the module).
    },
    enter_order=("discharge_current", "charge_current"),
    restore_order=("discharge_current", "charge_current"),
    # A hold's peak cover gives the discharge current back.
    peak_first={ACTION_HOLD: ("discharge_current",)},
    # Lower limits never add import, so nothing needs to end by itself (`NO_EXPIRY`, the default).
    write_caps={"setting": DEYE_SETTING_WRITES},
    # A limit the app or another controller set meanwhile is theirs, also after a restart: never written back over.
    restore_own_only=True,
    # Every register write counts against the 48, the hand-backs too (they always run; the next start waits).
    count_restores=True,
    hardware_test_first=True,
    note="Limits only: the battery's max discharge current at 0 A to hold, its max charge current at 0 A to cover the"
    " house without storing, only while Time of Use is off. No grid charge or sale. Never run on a device; writes"
    " only after a passed hardware test.",
)

#: The restore value of a role that has no snapshot of its own (never read before: a select unavailable while its
#: switch is off, a service, a button).
NORMAL: Final[Mapping[str, Mapping[str, Native]]] = {
    "sigenergy": _SIGENERGY_NORMAL,
    "huawei": {"forcible": "stop"},
    "solax": {"trigger": True},
    "solinteg": _SOLINTEG_NORMAL,
    "ferroamp": {"command": "auto"},
    "ecoflow": {"modbus_control": False, "battery_mode": _EF_AUTOMATIC},
    # Both force powers at 0 once the force is stopped: never back to the value found (see the recipe).
    "solis": {"charge_power": 0.0, "discharge_power": 0.0},
}

CONTROLS: Final[tuple[BrandControl, ...]] = (
    SIGENERGY_CONTROL, SOLAREDGE_CONTROL, HUAWEI_CONTROL, SOLAX_CONTROL, SOLINTEG_CONTROL, FERROAMP_CONTROL,
    VICTRON_CONTROL, GOODWE_CONTROL, TESLA_CONTROL, FOXESS_CONTROL, ECOFLOW_CONTROL, MARSTEK_CONTROL, SOLIS_CONTROL,
    KOSTAL_CONTROL, FRONIUS_CONTROL, DEYE_CONTROL,
)


# --- the export limit of more brands (see the module) --------------------------------------------------------------


def _curtail(*enable: str) -> Callable[[float, int], tuple[Want, ...]]:
    """The one export action: the limit at 0, and the brand's enable (`enable`, where it has one) on. The orders put
    the value first and the enable last, and back the enable first, so the limit never acts at a value not meant."""
    wants = (Want("export_limit", 0.0), *(Want(name, True) for name in enable))
    return lambda _rate_kw, _watchdog_s: wants


def _zero_or_above(state: str | None) -> str | None:
    """A lower bound as compared: `zero`, `above_zero`, `None` when it reads no number."""
    try:
        value = float(state) if state is not None else math.nan
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return "zero" if value <= 0 else "above_zero"


# Victron: -1 is "no limit" and 0 "none at all", one watt apart: read to the watt, and -1 handed back exactly.
_VIC_FEED_IN = Role(
    "export_limit", "number", _key("system_ess_max_feed_in_power", "number"), scale=1000.0, step=1.0, tolerance=1.0,
    no_limit=-1.0,
)
VICTRON_EXPORT_CONTROL: Final = BrandControl(
    brand="victron",
    adapter=VICTRON,
    roles=(_VIC_FEED_IN,),
    states={ACTION_CURTAIL_EXPORT: _curtail()},
    enter_order=("export_limit",),
    restore_order=("export_limit",),
    own_states=VICTRON_CONTROL.own_states,
    kind="export",
    limit_point="grid",
    zero_stated=True,
    restore_own_only=True,
    count_restores=True,
    note="ESS max feed-in power (Settings/CGwacs/MaxFeedInPower, a setting on the GX): -1 no limit, >= 0 the system's"
    " feed-in, DC- and AC-coupled. It caps the battery's feed-in too. Not tested on a plant.",
)

# Sungrow (mkaiser's package): the limit acts only while its switch is on; its lower bound is the inverter's own.
_SG_EXPORT = Role("export_limit", "number", _package("uid_export_power_limit", "number"), scale=1000.0, step=100.0)
_SG_EXPORT_ENABLE = Role("export_enable", "switch", _package("sg_export_power_limit_switch", "switch"))
_SG_EXPORT_MIN = Role(
    "limit_min", "sensor", _package("sg_export_power_limit_min"), restored=False, normalize=_zero_or_above,
)
SUNGROW_EXPORT_CONTROL: Final = BrandControl(
    brand="sungrow",
    adapter=SUNGROW,
    roles=(_SG_EXPORT, _SG_EXPORT_ENABLE, _SG_EXPORT_MIN),
    states={ACTION_CURTAIL_EXPORT: _curtail("export_enable")},
    enter_order=("export_limit", "export_enable"),
    restore_order=("export_enable", "export_limit"),
    # An inverter whose least limit is above 0 cannot stop its export: nothing is written.
    requires=(
        ("limit_min", frozenset({"zero", "above_zero"}), "limit_min_unread"),
        ("limit_min", frozenset({"zero"}), "limit_min_above_zero"),
    ),
    kind="export",
    limit_point="grid",
    # The package's own scene "Zero Export Power": the switch on and the number at 0.
    zero_stated=True,
    restore_own_only=True,
    count_restores=True,
    note="Export power limit (13073, W) with its switch (13086); needs the Sungrow meter. It caps the battery's"
    " export too. One firmware was reported to take 13073 as 0.1 %: 0 is no export either way. Not tested on a"
    " plant.",
)

# Solis: the number echoes at once, so the limit is read back by the sensor of the same register.
_SOLIS_BACKFLOW = Role(
    "export_limit", "number", _key("solis_modbus_inverter_backflow_power", "number"), scale=1000.0, step=1.0,
    parts=(_key("solis_modbus_inverter_backflow_power", "sensor"),), compose=_solis_register,
)
_SOLIS_BACKFLOW_ENABLE = Role("export_enable", "switch", _key("43073_4", "switch"))
#: "Flexible Export Status" (43292 with 43291): anything but `Off` and the inverter follows the flexible export
#: limit instead of the backflow power, so SpotNav's limit would not act.
_SOLIS_FLEXIBLE = Role(
    "flexible_export", "sensor", _key("solis_modbus_inverter_flexible_export_status"), restored=False
)
SOLIS_EXPORT_CONTROL: Final = BrandControl(
    brand="solis",
    adapter=SOLIS,
    roles=(_SOLIS_BACKFLOW, _SOLIS_BACKFLOW_ENABLE),
    states={ACTION_CURTAIL_EXPORT: _curtail("export_enable")},
    enter_order=("export_limit", "export_enable"),
    restore_order=("export_enable", "export_limit"),
    own_states=SOLIS_CONTROL.own_states,
    watched=((_SOLIS_FLEXIBLE, "Off", "flexible_export_on"),),
    kind="export",
    limit_point="grid",
    restore_own_only=True,
    count_restores=True,
    note="Backflow Power (43074, W) with the grid feed-in power limit switch (43073 bit 4); needs the meter or CT."
    " Not started while the inverter's Flexible Export is on. Nothing states that 0 means no export: checked on"
    " the plant first. Not tested on a plant.",
)

# GoodWe: the number is core's; the switch that enables the limit (47509) exists only in the HACS fork, whose switches
# carry no `goodwe-` before the key. Neither polls: Home Assistant's echo, watched after a hand-back.
_GW_EXPORT = Role(
    "export_limit", "number", _goodwe("grid_export_limit", "number"), scale=1000.0, step=1.0, readback="echo",
    settle_s=900.0,
)
_GW_EXPORT_ENABLE = Role(
    "export_enable", "switch", Match(("switch",), re.compile(r"^grid_export_limit_switch-[^-]+$")), readback="echo",
    settle_s=900.0, optional=True,
)
GOODWE_EXPORT_CONTROL: Final = BrandControl(
    brand="goodwe",
    adapter=GOODWE,
    roles=(_GW_EXPORT, _GW_EXPORT_ENABLE),
    states={ACTION_CURTAIL_EXPORT: _curtail("export_enable")},
    enter_order=("export_limit", "export_enable"),
    restore_order=("export_enable", "export_limit"),
    own_states=GOODWE_CONTROL.own_states,
    kind="export",
    limit_point="grid",
    restore_own_only=True,
    count_restores=True,
    hacs_roles=("export_enable",),
    hacs_repo=GOODWE_CONTROL.hacs_repo,
    note="Grid export limit (47510, W; % on some models: 0 is none in both) and, with the HACS integration, the"
    " switch that enables it (47509). With core alone the limit acts only where it is already enabled on the"
    " inverter: one that is not shows as a limit not obeyed. Nothing states that 0 means no export: checked on the"
    " plant first. Not tested on a plant.",
)

_SX_EXPORT = Role(
    "export_limit", "number", _key("export_control_user_limit", "number"), scale=1000.0, step=100.0
)
SOLAX_EXPORT_CONTROL: Final = BrandControl(
    brand="solax",
    adapter=SOLAX,
    roles=(_SX_EXPORT,),
    states={ACTION_CURTAIL_EXPORT: _curtail()},
    enter_order=("export_limit",),
    restore_order=("export_limit",),
    own_states=SOLAX_CONTROL.own_states,
    kind="export",
    limit_point="grid",
    restore_own_only=True,
    count_restores=True,
    note="Export Control User Limit (0x42, W): a setting register with no switch of its own. Nothing states that 0"
    " means no export: checked on the plant first. Not tested on a plant.",
)

#: The export-limit controls (`EXPORT_LIMIT_TABLE` lists every brand looked at, mapped or not). Each is the brand's
#: one number at the grid point, with at most one switch that enables it.
EXPORT_CONTROLS: Final[tuple[BrandControl, ...]] = (
    SIGENERGY_EXPORT_CONTROL, VICTRON_EXPORT_CONTROL, SUNGROW_EXPORT_CONTROL, SOLIS_EXPORT_CONTROL,
    GOODWE_EXPORT_CONTROL, SOLAX_EXPORT_CONTROL,
)


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


def field_tested(brand: str | None) -> bool:
    """Whether the brand's recipe (its battery control, else its export control) was tested on a real plant."""
    control = next((control for control in CONTROLS if control.brand == brand), None) or export_control_for(brand)
    return control is not None and control.field_tested


@dataclass(frozen=True, slots=True)
class Blocked:
    """A recipe this release holds back (shadow only, whatever a site passed): the brand, and where given only on a
    firmware key containing `firmware` or only at the recipe revision `recipe_rev`."""

    brand: str
    firmware: str | None = None
    recipe_rev: int | None = None
    #: Why, for the log and the release notes.
    note: str = ""


#: The release's block list (`blocked`): empty while no brand, firmware or revision is known to misbehave.
BLOCKED: Final[tuple[Blocked, ...]] = ()


def blocked(control: BrandControl, firmware_key: str, entries: tuple[Blocked, ...] | None = None) -> bool:
    """Whether this release holds the recipe back on a plant with this firmware key (`BLOCKED`)."""
    return any(
        entry.brand == control.brand
        and (entry.firmware is None or entry.firmware in firmware_key)
        and (entry.recipe_rev is None or entry.recipe_rev == control.recipe_rev)
        for entry in (BLOCKED if entries is None else entries)
    )


def hardware_test_first(brand: str | None) -> bool:
    """Whether the brand's recipe writes only once a person's hardware test passed on the plant's firmware (not yet
    `field_tested`)."""
    control = next((control for control in CONTROLS if control.brand == brand), None)
    return control is not None and control.hardware_test_first and not control.field_tested


#: The brands whose recipe was gated by `hardware_test_first` after control could already run on them (2026-11-25).
#: A plant of one whose control ran before (switched on, with a passed routine check on its firmware key now) counts
#: as passed and as having its control code (`BatteryControl._grandfather_running`): control never stops by an update.
GATED_AFTER_RUNNING: Final = frozenset(
    {"solaredge", "huawei", "solax", "solinteg", "victron", "goodwe", "tesla", "ferroamp"}
)


def export_control_for(brand: str | None) -> BrandControl | None:
    """The brand's export-limit control, `None` for a brand SpotNav has none for."""
    return next((control for control in EXPORT_CONTROLS if control.brand == brand), None)


#: The brands whose export limit the battery opt-in armed before the site's own switch came (2026-11-25): a site of
#: one whose battery control writes then gets the switch turned on once (`export_shell.ExportShell.migrate`), so
#: nothing stops by an update. A brand whose export control is newer than the switch is never armed that way.
EXPORT_ARMED_BY_BATTERY: Final = frozenset({"sigenergy"})


def export_field_tested(brand: str | None) -> bool:
    """Whether the brand's export-limit control was tested on a real plant: until then it writes only on a beta
    tester's site (`control.BatteryControl.export_state`). Never the battery recipe's flag: the two are apart."""
    control = export_control_for(brand)
    return control is not None and control.field_tested


#: The brands looked at for a local export limit (2026-11-25; each source at the commit named). `mapped`: SpotNav has
#: the control (`EXPORT_CONTROLS`), whose entities were read again in the source at that commit; every other row is
#: from the research notes only and is not written. `zero`: whether a source states that 0 means no export.
#: `verdict`: `beta` (mapped, untested: a beta tester's site only), `second_wave` (a recipe is possible, not built),
#: `later`, `not_now` or `not_possible` (no limit at the grid point: never an export control).
EXPORT_LIMIT_TABLE: Final[tuple[Mapping[str, Any], ...]] = (
    {"brand": "sigenergy", "integration": "sigen (TypQxQ 2.0.3, 41182ec)", "mapped": True, "verdict": "beta",
     "entity": "number `plant_grid_point_maximum_export_limitation` (Grid Export Limitation, kW 0-100, register"
     " 40038; `number.py` 185-199)",
     "zero": "stated", "no_limit": "never set reads `unknown`; no entity turns the limitation off",
     "note": "Every EMS mode, no Remote EMS needed; needs the plant's grid sensor; disabled by default. A limit"
     " never set cannot be handed back, so such a plant is not curtailed."},
    {"brand": "victron", "integration": "victron_gx / victron_mqtt (library victron_mqtt 3fe6d08)", "mapped": True,
     "verdict": "beta",
     "entity": "number `system_ess_max_feed_in_power` (Settings/CGwacs/MaxFeedInPower, W, -1 to 1000000;"
     " `_victron_topics.py` 3421-3433)",
     "zero": "stated", "no_limit": "-1",
     "note": "A setting stored on the GX; caps the battery's feed-in too. The community Modbus integration"
     " (sfstar/hass-victron, `settings_ess_maxfeedinpower`, register 2706) scales -1 badly and has no control."},
    {"brand": "sungrow", "integration": "mkaiser Sungrow-SHx YAML package (e2adb62), not an integration",
     "mapped": True, "verdict": "beta",
     "entity": "template number `uid_export_power_limit` (W, register 13073; `modbus_sungrow.yaml` 1750-1771) with"
     " modbus switch `sg_export_power_limit_switch` (13086, 1423-1436) and sensor `sg_export_power_limit_min`"
     " (5621, 471-482)",
     "zero": "stated (the package's scene Zero Export Power)", "no_limit": "the switch off",
     "note": "Needs the Sungrow meter; caps the battery's export too; refused where the inverter's least limit is"
     " above 0. One firmware took 13073 as 0.1 %."},
    {"brand": "solis", "integration": "solis_modbus (Pho3niX90 840ab30)", "mapped": True, "verdict": "beta",
     "entity": "number `solis_modbus_inverter_backflow_power` (Backflow Power, W, register 43074;"
     " `sensor_data/hybrid_sensors.py` 2607-2622) with switch `43073_4` (Grid feed in power limit switch;"
     " `sensor_data/switch_sensors.py` 24-29)",
     "zero": "not stated", "no_limit": "the switch off",
     "note": "Needs the meter or CT. Flexible Export (43292/43291) overrides it when on: not started then. Not"
     " solax_modbus's `backflow_power_switch`, which writes the whole register."},
    {"brand": "goodwe", "integration": "core goodwe (library 0.4.10, 66dfcdd) and the HACS fork (mletenay e6e9b38)",
     "mapped": True, "verdict": "beta",
     "entity": "number `goodwe-grid_export_limit-<serial>` (W on ET/EH, register 47510; % on other models) with the"
     " fork's switch `grid_export_limit_switch-<serial>` (47509; fork `switch.py` 39-45, 122)",
     "zero": "not stated", "no_limit": "the switch off (the fork only)",
     "note": "An installer parameter. Core has no enable switch: the limit then acts only where it is already"
     " enabled. The library ignores a negative value."},
    {"brand": "solax", "integration": "solax_modbus (wills106 4acd644)", "mapped": True, "verdict": "beta",
     "entity": "number `<hub>_export_control_user_limit` (Export Control User Limit, W, register 0x42;"
     " `plugin_solax.py` 2866-2909)",
     "zero": "not stated", "no_limit": "none (the model's maximum)",
     "note": "A setting register; its range differs per generation. Not `grid_export_limit` (a remote-control"
     " target) nor `export_duration`."},
    {"brand": "solaredge", "integration": "solaredge_modbus_multi (WillCodeForCats 39567a9)", "mapped": False,
     "verdict": "second_wave",
     "entity": "number `<uid>_site_limit` (W) with selects `limit_control_mode` and `limit_control`",
     "zero": "stated (may still export small amounts)", "no_limit": "the mode Disabled",
     "note": "Only with the integration's option for site limit control (off by default); needs the SolarEdge"
     " meter. Never `active_power_limit`, a cap on the AC output."},
    {"brand": "huawei", "integration": "huawei_solar (wlcrs d8b0120)", "mapped": False, "verdict": "second_wave",
     "entity": "services `set_zero_power_grid_connection`, `set_maximum_feed_grid_power`,"
     " `reset_maximum_feed_grid_power`",
     "zero": "stated (mode 5)", "no_limit": "mode 0",
     "note": "Read back only through a sensor disabled by default; the hand-back is a mode, a power and a share."},
    {"brand": "tesla", "integration": "core tesla_fleet / teslemetry", "mapped": False, "verdict": "second_wave",
     "entity": "select `components_customer_preferred_export_rule` (`never`, `battery_ok`, `pv_only`)",
     "zero": "by its label only", "no_limit": "the option found",
     "note": "A cloud control with few writes a day; only for sites with a battery and solar."},
    {"brand": "deye", "integration": "solarman (davidrapan 84982c5); kellerza MQTT", "mapped": False,
     "verdict": "second_wave",
     "entity": "number Export Surplus Power / Max Sell Power with switch Export Surplus / Solar Sell (single"
     " phase); a number alone on three phases",
     "zero": "not stated", "no_limit": "the switch off",
     "note": "The unit differs between low- and high-voltage models; nobody states the wear of the register."},
    {"brand": "foxess", "integration": "foxess_modbus (nathanmarlor 94252f0)", "mapped": False,
     "verdict": "second_wave", "entity": "number `export_power_limit` (W): KH 1.33+, H3 Smart, H3 Pro only",
     "zero": "not stated", "no_limit": "not stated", "note": "H1 and H3 classic have no entity."},
    {"brand": "ecoflow", "integration": "ef_powerocean_tcpmodbus (56331de)", "mapped": False,
     "verdict": "second_wave", "entity": "switch `grid_feed`",
     "zero": "stated", "no_limit": "the switch on",
     "note": "Acts only while the integration's Modbus control runs, the battery recipe's own role."},
    {"brand": "solinteg", "integration": "solax_modbus `plugin_solinteg.py` (4acd644)", "mapped": False,
     "verdict": "second_wave",
     "entity": "select `export_limit_switch` with number `export_limit_value` (% of rated)",
     "zero": "not stated", "no_limit": "the select off",
     "note": "The cloud was seen to write over Modbus limits after about an hour."},
    {"brand": "growatt", "integration": "growatt_modbus; growatt_local; solax_modbus Growatt", "mapped": False,
     "verdict": "later", "entity": "select `export_limit_mode` with number `export_limit_rate` (%)",
     "zero": "not stated", "no_limit": "the mode off",
     "note": "No export recipe brand yet; an enabled limit at 100 % was seen to block the battery's export."},
    {"brand": "sma", "integration": "pysmaplus (5d74e78)", "mapped": False, "verdict": "not_now",
     "entity": "service `pysmaplus.set_value` on Active Power Limitation (GCP)",
     "zero": "stated", "no_limit": "not stated",
     "note": "A limited number of parameter writes over the inverter's life."},
    {"brand": "fronius", "integration": "core fronius", "mapped": False, "verdict": "not_possible", "entity": None,
     "zero": None, "no_limit": None,
     "note": "Only `ac_power_limit`, a cap on the inverter's whole AC output (the house would be fed from the"
     " grid); the feed-in limit is in the web UI only."},
    {"brand": "kostal", "integration": "core kostal_plenticore", "mapped": False, "verdict": "not_possible",
     "entity": None, "zero": None, "no_limit": None,
     "note": "Only `active_power_limitation`, a cap on the inverter's AC output that takes priority over the"
     " battery's control."},
    {"brand": "enphase", "integration": "core enphase_envoy", "mapped": False, "verdict": "not_possible",
     "entity": None, "zero": None, "no_limit": None, "note": "Only the grid relay, which islands the site."},
    {"brand": "ferroamp", "integration": "ha-ferroamp", "mapped": False, "verdict": "not_possible",
     "entity": None, "zero": None, "no_limit": None, "note": "No export limit entity."},
    {"brand": "marstek", "integration": "marstek_venus_modbus; marstek_local_api", "mapped": False,
     "verdict": "not_possible", "entity": None, "zero": None, "no_limit": None, "note": "No export limit entity."},
)


def table() -> list[dict[str, Any]]:
    """The command table, per brand and action, for diagnostics and the report."""
    rows: list[dict[str, Any]] = []
    for control in CONTROLS:
        for action in CONTROL_ACTIONS:
            if action not in control.states:
                continue
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
        rows.append({
            "brand": control.brand, "action": ACTION_CURTAIL_EXPORT, "writes": {w.role: w.value for w in wants},
            "enter_order": list(control.enter_order), "restore_order": list(control.restore_order),
            "limit_point": control.limit_point, "zero_stated": control.zero_stated,
            "field_tested": control.field_tested,
            "no_limit": {role.name: role.no_limit for role in control.roles if role.no_limit is not None},
            "optional": sorted(control.optional_names()),
        })
        rows.append({"brand": control.brand, "action": ACTION_SELF_CONSUME, "writes": {"*": SNAPSHOT}})
    return rows


def service_call(
    method: RoleMethod, entity_id: str | None, value: Native, *, device_id: str | None = None,
    services: Mapping[str, str] | None = None, watchdog_s: int = WATCHDOG_S, device_field: str = "device_id",
    duration: bool = False, power_field: str = "power",
    duration_field: str = "duration",
) -> tuple[str, str, dict[str, Any]]:
    """The Home Assistant service call `(domain, service, data)` that writes `value` to a role.

    Numbers, selects, switches and buttons use HA's own entity services (core `number.set_value`,
    `select.select_option`, `switch.turn_on`/`turn_off`, `button.press`). A service role (Huawei's forcible charge,
    Ferroamp's requests) turns `charge:<W>` / `discharge:<W>` / a kind without a power (`stop`, `auto`) into its
    integration's service, for the battery's device in the role's `device_field`, with the power where the value has
    one and, where the role is a lease (`Role.lease`, passed as `duration`), the window as the duration in
    minutes."""
    if method == "number":
        return "number", "set_value", {"entity_id": entity_id, "value": value}
    if method == "select":
        return "select", "select_option", {"entity_id": entity_id, "option": value}
    if method == "switch":
        # A restore writes the state found ("on"/"off" as read), an action a bool.
        on = value is True or (isinstance(value, str) and value.strip().lower() in ("on", "true"))
        return "switch", "turn_on" if on else "turn_off", {"entity_id": entity_id}
    if method == "button":
        return "button", "press", {"entity_id": entity_id}
    if not isinstance(value, str) or services is None or (device_field and device_id is None):
        raise ValueError(f"no service call for {value!r}")
    kind, _, power = value.partition(":")
    full = services.get(kind)
    if full is None:
        raise ValueError(f"no service for {kind!r}")
    domain, service = full.split(".", 1)
    # An empty device field: the integration finds its one device itself (Solis's services take no device).
    data: dict[str, Any] = {device_field: device_id} if device_field else {}
    if power:
        data[power_field] = int(power)
        if duration:
            data[duration_field] = max(1, min(1440, watchdog_s // 60))
    return domain, service, data
