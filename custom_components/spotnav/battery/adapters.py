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
* SolarEdge Modbus, core and binsentsu's HACS `solaredge_modbus` (see `SOLAREDGE_MODBUS`): facts only, no control.
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
* Marstek (Venus E, C, D, A), read 2026-11-23 (`plans/v3_marstek_adapter.md`), one adapter for two integrations, read
  only (no control is mapped). Each finds only its own entities (the battery entity's platform), so the keys of both
  sit in one match where they differ:
  * `marstek_local_api` (jaapp/ha-marstek-local-api `624881c`, manifest 1.2.0.rc7; Marstek's local Open API over UDP,
    Rev 1.0; line numbers in `sensor.py`): unique ids are `<mac>_<key>` per battery (lines 644, 694) and, when one
    entry holds several batteries, `<the macs sorted, joined by _>_<key>` on a "Marstek System" device (lines 589-590,
    748). The level is `battery_soc` (Bat.GetStatus `soc`, line 70) or, on the system device,
    `system_average_soc` (line 465), which weighs each battery by its rated capacity (`coordinator.py` lines
    138-148). The size is `battery_rated_capacity` (`rated_capacity`, Wh, shown in kWh, line 97; no firmware scaling
    applies to it, `compatibility.py` lines 98-154) or `system_total_rated_capacity`, their sum (line 439). The
    firmware is `firmware_version` (Marstek.GetDevice `ver`, line 336; the device's `sw_version` is the same number).
    The mode is the sensor `operating_mode` (ES.GetMode `mode`, line 371; "Auto", "AI", "Manual" or "Passive",
    `const.py` lines 67-72; MarstekEnergy/ha_marstek 0.3.0 knows "UPS" too), free only in "Auto" (the battery's own
    self-consumption on its CT): "AI" is Marstek's own optimiser, "Manual" its time slots and "Passive" a setpoint some
    outside controller sends. The mode is read every fifth poll (300 s by default, `const.py` line 17), and the system
    device has none: with several batteries in one entry it is not found and SpotNav cannot tell who runs them. No
    rating, floor or lifetime counter of the battery is an entity (the API has none), so the ratings are learnt; the
    CT's `a_power` to `c_power` are the grid's, not the battery's.
  * `marstek_modbus` (ViperRNMC/marstek_venus_modbus 2026.10.1, `cb5a241`; the 2026.11.1 betas keep the scheme,
    `entity.py` line 77): unique ids are `<config entry id>_<key>` (`sensor.py` line 62, `select.py` line 106,
    `switch.py` line 71, `number.py` line 71), the keys from `registers/e_v12.yaml` (Venus E v1/v2, C), `e_v3.yaml`
    (Venus E v3), `d.yaml` and `a.yaml`, one entry per battery. The level is `battery_soc` (32104, or 34002 on E v3).
    The size is `battery_total_energy` (32105, kWh, e.g. `e_v12.yaml` line 75), which the integration itself takes
    for the capacity (its `stored_energy` is the SoC times it, line 973, and its cycle count the discharge over it),
    as does ffunes/Marstek-Venus-Energy-Manager. The floor is the number `discharging_cutoff_capacity` (44001, %,
    E v1/v2 only, line 692, disabled by default). The mode is the select `user_work_mode` (43000, line 448: `manual`,
    `anti_feed` "Self Consumption", `trade_mode` "AI Optimization"), free only in `anti_feed`, and the switch
    `rs485_control_mode` (42000, line 566, disabled by default), free while off: on, an outside controller writes the
    battery's force mode and powers (evcc keeps `user_work_mode` at `anti_feed` meanwhile). The firmware is the
    computed `firmware_version` (line 930, "V<ems>.<bms>" or "V<ems>.<vms>.<bms>"). The battery's counters are
    `total_charging_energy` and `total_discharging_energy` (33000/33002, kWh, lines 187/198; on Venus A and D with
    their own PV input they may count PV energy to the house too, the integration's README says). No rating is read:
    `max_charge_power` and `max_discharge_power` (44002/44003) are settings an outside controller writes (evcc writes
    0 there to hold), not the battery's rating, so the ratings are learnt. The battery's power `battery_power`
    (32102, or 30001) is charge-positive (`site_detection.BATTERY_ROWS`).
  * Not read: the official `marstek` (MarstekEnergy/ha_marstek `6394e6a`, 0.3.0, `<stable id>_<key>`, `entity.py` line 32).
    Its keys (`battery_soc`, `battery_rated_capacity`, the lower-case `device_mode`) would match, but its
    `battery_power` is the library's `abs(bat_power)` (aiomarstek 0.2.2, `models.py` line 355), an unsigned magnitude with no
    direction, which the site's power cannot be read from (the direction is in the separate `battery_status`, which no
    site row reads), so no battery entity of it is found. Read 2026-11-23 (`plans/research_integrations/marstek_control_2026-11-23.md`).

* EcoFlow PowerOcean, read 2026-11-23 (`plans/research_integrations/ecoflow_control_2026-11-23.md`), one adapter for two
  integrations, read only (no control is mapped; both write a battery setpoint under a heartbeat that SpotNav does not
  send). `ef_powerocean_tcpmodbus` (MaxGrmm/EF-PowerOcean-TcpModbus `56331de`, 2.10.1, local Modbus TCP): unique id
  `<config entry id>_<key>` (`entity.py` line 38), one entry per inverter, the keys from `definitions.py`: the level
  `battery_soc` (line 200), the size `battery_capacity` (40552, Wh, line 241), the counters `bat_charged_total` and
  `bat_discharged_total` (lines 585, 594), the app's reserve as the integration holds it `battery_reserve_effective`
  (line 757, %). The mode is the enum sensor `operating_mode` (`standby`, `self_consumption`, `ai`, `telemetry.py` line
  227), free only in `self_consumption`, and the binary sensor `modbus_control` (the heartbeat holds the control, line
  718), free while off. `ecoflow_energy` (shuette42/ecoflow-energy-ha `771b646`, 1.28.0): `<device sn>_<key>` (`sensor.py`
  line 337), with `soc_pct` (the system level, `stream_proto.py` line 88, `powerocean_modbus.py` line 163),
  `batt_charge_energy_kwh` and `batt_discharge_energy_kwh`, and the cloud select `work_mode` (`self_use` free,
  `ai_schedule` not; STREAM's `self_powered` free) and the switch `modbus_control`. The device's serial has no
  underscore, so the sensors are matched `^<sn>_<key>$`: the entry also carries `unit_soc_pct`, `unit_batt_w` and the
  per-inverter `inverter_<n>_...` of single units, which are not the system's. The integration states no size
  (`batt_remaining_wh` is the energy left). Neither states a rating: `battery_charge_power_limit` and
  `battery_discharge_power_limit` are the BMS's capability at the moment (or an app schedule's cap) and `limit_inv_power`
  the converter's AC rating, so the ratings are learnt. The battery's power is charge-positive on both
  (`battery_power`, 40525: the control's own sign is +1 for charge, `powerocean/const.py` line 170; `batt_w`, 0x020C:
  "positive = charging", `stream_proto.py` line 28; `site_detection.BATTERY_ROWS`). The firmware is the device's
  `sw_version`. The inverter has a heartbeat dead-man of its own (60 s) and the EcoFlow app locks while Modbus control
  is on; none of it is written here.

* Kostal Plenticore (plus, G1 to G3, with a BYD battery), read 2026-11-23 (`plans/research_integrations/kostal_control_2026-11-23.md`),
  one adapter for two integrations, read only (the control is Modbus with an installer's setting and a timeout the
  integrations renew; none of it is written here). Core `kostal_plenticore` (home-assistant/core `42f4a32`, pykoplenti
  REST): sensors `<entry id>_<module id>_<data id>` (`sensor.py` line 867), the level `devices:local:battery_SoC`
  (line 237) and the battery power `devices:local:battery_P` (line 229); the module part is why the match is on
  `battery_soc` after `_` or `:`, and why `P` alone is never matched. The inverter's own floor is the number
  `devices:local_Battery:MinSoc` (`number.py` line 50, 5-100 %, disabled by default). The firmware is the device's
  `sw_version` ("IOC: .. MC: ..", `coordinator.py` line 107). Core states no size and no rating; its charge counters are
  two (`EnergyChargeGrid` and `EnergyChargePv`, kWh, from the grid and from the panels) beside one discharge counter, so
  no counter pair is read. The mode select `devices:local` (`select.py` line 104: "None", `SmartBatteryControl`,
  `TimeControl`) reads "None" when the inverter's smart or timed control is off, which SpotNav takes for no reading
  (`facts._NO_READING`), so core alone reads the mode as unknown. HACS `kostal_modbus_control` (mbuhansen
  `3bd84ac`, 4.3.0, Modbus TCP): `<config entry id>_<key>` (`sensor.py` line 269), `battery_soc` (register 514),
  `battery_work_capacity` (1068, Wh, `sensor.py` line 844, disabled by default; its own Predbat sensor says the
  inverter's figure may be the gross size, so the size is the integration's own and not more), `software_version`
  (the inverter's UI/SW, `sensor.py` line 1044, disabled by default) and `battery_mgmt_mode` (register 1080, "No external
  battery management", "..via digital I/O" or "..via Modbus protocol", line 982, disabled by default), free only in the
  first. Its `battery_max_charge_power_limit` and `battery_max_discharge_power_limit` are registers 1038 and 1040, the
  limits the integration itself writes (0 to block, `const.py` lines 37-38), not the battery's 1076/1078 rating: not read.
  The battery's power is discharge-positive on both, negated by the site (core: Predbat's template uses
  `scb_battery_power` as the discharge-positive power without inverting it, `templates/kostal.yaml` line 127; Modbus:
  register 582, "negative=charge", `const.py` line 124): sign unverified on hardware.

* Fronius (Gen24, Gen24 Plus, Tauro, Verto Plus; BYD or Reserva battery), core `fronius` (home-assistant/core `42f4a32`,
  read 2026-11-23, `plans/research_integrations/fronius_control_2026-11-23.md`): read only (the Modbus limits and
  their revert timer are not written here). Its unique ids use a hyphen, `<serial>-<key>` for the storage's sensors
  (`sensor.py` line 1266: `state_of_charge`, line 1069; `capacity_designed`, line 1033; only for a storage that reports
  a serial) and `<inverter id>-modbus-<key>` for what the Modbus settings read (lines 1200-1202): the counters
  `storage_energy_charged_total` and `storage_energy_discharged_total` (Wh, DC side, line 512), `battery_power_limit_reference`
  (WChaMax, W, line 545, the base the integration's percent limits are of, one figure for charge and discharge), and the
  number `battery_minimum_reserve` (`number.py` line 91, MinRsvPct, the discharge floor). They exist only where the
  inverter accepts control over Modbus. The mode is the two switches `battery_charge_power_limit_enabled` and
  `battery_discharge_power_limit_enabled` (`switch.py` lines 61-69, SunSpec `StorCtl_Mod`): both off is the inverter's own
  self-consumption; a limit in force is someone's. Schedules and the Energy Cost Assistant in the inverter's web UI are
  invisible to Home Assistant. The battery's power is discharge-positive (`power_battery`, disabled by default,
  `sensor.py` line 926; its `power_battery_discharge` is the positive part, line 936), negated by the site. The firmware
  is the device's `sw_version`, which on the SolarNet device is the logger's software, not the inverter's. The size is
  the storage's *designed* capacity, not the usable or aged one; `capacity_maximum` (line 1026) is not read.

* Homevolt (Tibber, Polarium), core `homevolt` (home-assistant/core `42f4a32`, pyHomevolt 0.7.0 `634bcbc`, read 2026-11-23,
  `plans/research_integrations/homevolt_control_2026-11-23.md`): read only. Unique ids are `<ecu id>_<key>` (`sensor.py`
  line 324; `number.py`, `select.py`, `switch.py` the same with the entity's own key), and a sensor's key is the
  library's display key with spaces and capitals (`homevolt.py` lines 815-860: `State of Charge`, `Power`), so the match
  is on the lower-cased `<12 hex>_state of charge`; the per-blade `Homevolt battery <n> ...` and the external CTs'
  `Power <type>` end otherwise and are not the system's. The level is the system's `State of Charge` (the library divides
  `soc_avg` by 100, which the one fixture reads as %, and the vendor's own example reads as whole percent: scale
  unverified on a unit). The mode is the switch `local_mode` (`switch.py` line 305): Tibber's cloud schedules (Smart
  Control, Grid Rewards) replace anything local while it is off, so the battery is the person's only while it is on. The
  unit has no self-consumption mode of its own to compare a schedule type with. Nothing else is read: core exposes no
  size (the vendor's `rated_capacity` is in `/ems.json`, which the library does not read; `Available Charging Energy` plus
  `Available Discharge Energy` is a two-sensor sum that one example supports and nothing else), no rating
  (`Available ... Power` is what is available now), no firmware, and no battery counters (`Energy imported` and
  `exported` are the system aggregate through the inverter, solar pass-through and losses included). The battery's power
  is the EMS `Power`, disabled by default, discharge-positive per the vendor's API document, negated by the site: sign
  unverified on hardware (the library's fixture reads -17 at idle).

* Deye and Sunsynk hybrids (the Deye profiles `deye_hybrid` 1p, `deye_p3` 3p LV and HV), read 2026-11-23
  (`plans/research_integrations/deye_sunsynk_control_2026-11-23.md`), read only (the control is the time-of-use
  programmes and the grid-charge settings, which stay as registers with no timeout). `solarman` (davidrapan/ha-solarman
  `84982c5`; the domain is shared with other makers' profiles that name their items alike, `solis_hybrid`, `sofar_hybrid`
  and more, so only a device whose manufacturer is "Deye", which the profile's `info` gives the device registry,
  `common.py` line 167, or "Sunsynk" is read): unique id `<entry id>_<slug of the item's name>_<platform>` (`entity.py`
  line 74, `common.py` lines 217-220): the level `battery_sensor` (the item "Battery"), the counters
  `total_battery_charge_sensor` and `total_battery_discharge_sensor` (kWh, at the battery's register; the high-voltage
  scale follows the firmware) and the size `battery_capacity_sensor`, an estimate the integration learns from the level
  and the counters while the battery charges and discharges (`sensor.py` line 134; unknown at first). The Ah number
  `battery_capacity_number` is not read: a setting the person types (up to 2000 Ah) that nothing ties to the real bank,
  as Solis's. The mode is the select `time_of_use_select`: "Disabled" is the inverter without its programmes, every
  weekday choice runs them. Nothing says a rating: there is no battery power limit, only the settings
  `battery_max_charging_current` and `battery_max_discharging_current` (A, which a hold would write) and the inverter's
  `device_rated_power`; no floor is read either (`Battery Low SOC` and the shutdown and restart levels are the
  inverter's alarms, not a discharge floor); the firmware is an attribute of the `Device` sensor, not an entity. The
  battery's power `battery_power_sensor` is the raw signed register, discharge-positive (the integration's own capacity
  learner takes `power < 0` as charging, `sensor.py` line 141), which the existing site row negates. kellerza/sunsynk
  (`49efe3f`, MQTT add-on): unique id `<inverter serial>_<slug of the sensor's name>` (`a_sensor.py` line 133) on the
  platform `mqtt`, which every MQTT device shares, so it is read only on a device whose manufacturer the add-on's option
  gives as "Sunsynk" (its default) or "Deye": `battery_soc`, `total_battery_charge` and `total_battery_discharge` (the
  three-phase definitions), the time-of-use switch `use_timer` (1p) or `prog_time_of_use_enabled` (3p) and
  `battery_power`, a signed register with the same sign (`micro_hybrid_ess.py` line 31: "positive = discharging").

* Anker SOLIX Solarbank Max AC, Max, XE and 4 E5000 Pro, `anker_solix_official` (anker-charging/ha-anker-solix-official
  `a9930d7`, 1.5.1, local Modbus TCP, read 2026-11-23, `plans/research_integrations/anker_solix_control_2026-11-23.md`):
  read only. One config entry per device; unique id `<config entry id>_<key>` (`base_entity.py` line 48), the keys from
  the model's YAML profile in `config/` (the same four in all four battery profiles): the level `battery_soc` (10014),
  the size `rated_energy` (10250, kWh), the firmware `device_sw_version`, the inverter's discharge floor
  `discharge_limit_soc` (60001, 0-20 %) and the select `operating_mode` (10064), free only in `self_consumption`: every
  other option is the Anker app's own (time-of-use, smart, custom, dynamic pricing) or someone's third-party control. The
  battery's power is two unsigned sensors split from one signed register, `battery_charging_power` from its negative
  values and `battery_discharging_power` from its positive (`power_split_mode`, the YAML's lines 112-135), the site's
  pair. Not read: the lifetime counters `cumulative_charge_energy` and `cumulative_discharge_energy` (the discharge one
  under-counts by about half on a Max AC on firmware 1.0.2.23, issue 181, open, so a round trip from them would be wrong);
  the backup reserve `backup_reserve_soc` (60002), which the unit holds (and grid-charges up to, at its full input power,
  also in self-consumption) only while the backup switch (60003) is on, a register that is no entity, so the value says
  nothing on its own; and any rating (`max_charge_power` and `max_discharge_power` are internal to the integration, not
  entities). The cloud integration `anker_solix` (thomluther) has no adapter: its sign and its totals are unstated.

* Zendure SolarFlow, Hyper, AIO and Hub, `zendure_ha` (Zendure/Zendure-HA `e67b04e`, read 2026-11-23,
  `plans/research_integrations/zendure_control_2026-11-23.md`): read only. The unique id is the device's name in the
  Zendure app and the property, `snakecase("<name lower>_<property>")` (`entity.py` lines 24-35 and 59-65), with no
  serial and no config entry id in it, and every device of the account is in one config entry. So an entity is found by
  the property at the end of the id and on the battery entity's own device first (`facts.find_entity`), which is what
  keeps two devices apart; a rename in the app renames the ids at the next load. A battery pack is a child device of
  its own. Per device: the level `electric_level` (`device.py` line 181), the size `total_kwh` (line 193, worked out
  from the packs' serial prefixes, 0 until they are seen and for an unknown prefix, which reads as no size), the rating
  of the output `inverse_max_power` (the device's `inverseMaxPower`, W, `entity.py` line 105; the output the device allows
  now, and one 2400 Pro firmware reported 800 W for a while), the floor `min_soc` (the number `minSoc`, shown 0-100 %, line
  159) and the counters `aggr_charge` and `aggr_discharge` (kWh, lines 200-201), which the integration adds up itself
  from the two pack power sensors, not counters of the device (they stand still while Home Assistant is down, both
  alike, and the round trip learns from their change). Not read: the charge rating, `charge_max_limit` (the device's
  `chargeMaxLimit`, W) is a plain raw sensor with no unit (`entity.py` line 380), which `facts.to_kw` would read as
  kW; and the firmware, which is the device's `sw_version` in the registry (`updateVersion`, line 408). Who runs it: the
  integration's own Zendure Manager, a device of its own (`select` `zendure_manager_operation`, `manager.py` line 106),
  rewrites the limits every few seconds from the P1 meter while it is not `off`; the Zendure app's HEMS takes the
  device over (`hems_state`, on for a minute after its last report, `device.py` line 190); and a state-of-charge
  calibration (`soc_status` 1) ignores any setpoint. The battery's power is the pair `output_pack_power` (into the
  battery) and `pack_input_power` (out of it), both non-negative (`device.py` lines 185-186 and 258-268), as the site
  detection's Zendure row reads them.

* Omnibattery, `omnibattery` (ffunes/Omnibattery `e60eb4f`, 1.6.1, read 2026-11-23): a controller that drives Marstek,
  Zendure, Anker, Sessy, Hoymiles and Indevolt batteries through its own drivers, so the batteries' entities are its
  own and no `marstek_modbus` or `zendure_ha` entity exists. Read only, and the whole system as one battery: its
  system entities keep the legacy unique id `marstek_venus_system_<key>` (`infra/entity_naming.py`
  `SYSTEM_UNIQUE_ID_PREFIX`, never changed; only the suggested entity id is `omnibattery_<key>`, an install from
  before the rebrand keeps `marstek_venus_system_<key>`), so they are found by the key at the end of the unique id.
  The level is `system_soc` (%, weighted by each battery's capacity, a plain mean where no capacity is known; batteries
  in manual mode are left out unless all are, `sensors/aggregate_sensors.py` `_calculate_system_soc`) and the size
  `system_total_energy` (kWh, the sum of the batteries' sizes). The counters are not read: the system has only the
  daily `system_daily_charging_energy` and `_discharging_energy`, which restart every day, and the batteries' own
  lifetime counters are one per battery. No rating and no floor is a system entity (learnt). Nobody else is read as
  running it: the integration is itself the outside controller (`coexist.py`), so no mode rule. The battery's power is
  `system_battery_cell_power`, the signed sum over the batteries, positive while charging, as the site detection's
  Omnibattery row reads it (see there).

* Sungrow SH hybrids (SH-RS 1p, SH-RT 3p) through mkaiser's YAML Modbus package (mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant
  `e2adb62`, read 2026-11-23, `plans/research_integrations/sungrow_control_2026-11-23.md`), read only (the mode and power
  registers have a watchdog only in External EMS, which nothing here writes). It is no integration: its entities are
  `modbus` sensors (unique ids `sg_<key>`) and `template` numbers and selects (`uid_<key>`), with no config entry and no
  device. `modbus` is every YAML Modbus device's platform, so the adapter is chosen by the battery entity's unique id
  too (`PackageScope`: `sg_` or `uid_`, an optional `_inv_<n>` suffix on every id of the multi-inverter files
  `modbus_sungrow_multiple_inverters_<n>.yaml`), and its entities are looked for in both platforms, the inverter the
  battery entity's suffix names. The level is `sg_battery_level` (13022, 0.1 %): the package's own comment says "0..100 %
  | min_soc..max_soc" and it builds `sg_battery_level_nom` (min + (max - min) x level) from it, so whether the register is
  the level inside the person's SoC window or the absolute level is not settled (a tester decides); it is read as it
  stands. The size is `uid_battery_capacity_high_precision` (5638, kWh). The rating is `sg_bdc_rated_power` (5627, the
  battery converter's rated power, W; evcc uses it as the battery's charge and discharge power), one figure for both
  directions. The counters are `sg_total_battery_charge` and `sg_total_battery_discharge` (kWh; the package's README
  says some firmware count the PV-to-battery path in the charge, which is the cells' energy, as wanted). The firmware is
  `sg_inverter_firmware_version` (13249, SH*T and newer only: it reads unavailable elsewhere, and then no firmware is
  known). The mode is the template selects `uid_ems_mode` (free only in "Self-consumption mode (default)") and
  `uid_battery_forced_charge_discharge` (free only at "Stop (default)"); the inverter's own time-of-use windows run
  beside self-consumption and are not seen by either. The battery's power `sg_battery_power` (5213) is
  discharge-positive ("negative: Battery charging"), negated by the site. No floor is read: the SoC limits
  (`uid_battery_min_soc`) are the person's, and what the level means against them is open.

* sonnenBatterie, `sonnenbatterie` (weltmeyer/ha_sonnenbatterie `9441b4a`, 2026.11.6, local API, read 2026-11-23,
  `plans/research_integrations/sonnen_control_2026-11-23.md`): read only. Sensors are `sensor.sonnenbatterie_<serial>_<key>`
  (the literal `sensor.` is inside the unique id, `sensor.py` line 108), numbers and selects `sonnenbatterie_<serial>_<key>`;
  the key is the entity's `legacy_key` where one is set, so the level (USOC, the user-visible one) is `state_charge_user`
  and the size `state_total_capacity_real` (Wh, disabled by default: the size is learnt until it is enabled). The rating
  `max_power` (W, the stack's or the inverter's, one figure for both directions) is anchored on the serial, as the
  `tou_max_power` setting also ends in it. The mode is the sensor `operating_mode` (`"1"` manual, `"2"` automatic
  self-consumption, `"10"` time-of-use, `"11"` optimization), free only in `"2"`: the others are the person's schedule,
  sonnen's own optimiser or a virtual power plant (Tibber), and a mode change is refused while a VPP is active. The floor
  is the reserve, the number `battery_reserve` (`EM_USOC`) and the sensor `backup_buffer`, the highest read. Not read: the
  lifetime counters `energy_battery_charge` and `energy_battery_discharge` (the integration integrates `Pac_total_W` from
  zero at install, skips what happens while Home Assistant is off and ignores up to 50 W, so they are no device meters
  and a round trip from them would be wrong); the grid status `systemstatus` (free text, its values are not listed in
  the code); the firmware, which is the device's `sw_version` (read from the device registry, not an entity). The
  battery's power is `state_battery_inout` (`Pac_total_W`), discharge-positive, the AC side (the README's table).

* Alpha ESS SMILE, `alphaess_modbus` (senalse/ha-alphaess-modbus `b256f55`, v1.15.0, local Modbus TCP, read 2026-11-23,
  `plans/research_integrations/alphaess_control_2026-11-23.md`): read only. Unique id `<config entry id>_<key>` on every
  platform, one entry per inverter, the device's manufacturer "AlphaESS". The level is `soc_battery` (%), the size
  `battery_capacity_kwh` (disabled by default: learnt until it is enabled), the counters `total_energy_charge_battery` and
  `total_energy_discharge_battery` (the `_from_grid` one ends otherwise), the firmware the `ems_version` sensor (the EMS
  that runs the dispatch; the device's `sw_version` is the fallback) and the floor `discharging_cutoff_soc` (the
  number and its sensor). The ratings are learnt: the registers give the BMS's present current limits and no voltage
  to multiply by is worked out. The modes are `dispatch_start` (1 while a dispatch runs: a Modbus controller's, an
  automation's, or a supplier's virtual power plant writing the same registers, evcc issue 36201) and
  `charging_time_period_control` (the app's grid-charge and discharge windows, 0 = none), each free only at 0. The
  battery's power is `power_battery` (register 0x0126), whose sign is DISPUTED: the vendor's register list says
  "-: Charge, +: Discharge", the integration's own house-load formula `pv + battery + grid` and evcc's template
  (which feeds the raw register as the battery's power) need discharge-positive, but its own description string and
  its "remaining time" sensor read positive as charging. Read as discharge-positive (the vendor's list); sign
  unverified on hardware. Not read: the cloud integration `alphaess` (CharlesGillanders), whose unique id is
  `<entry id>_<serial> - <English display name>`, a name a rename or a translation moves.

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
class PackageScope:
    """A YAML package's entities, which belong to no config entry and no device: found by the fixed unique ids the
    package gives them, over `platforms` (a package's numbers and selects are `template`, its registers `modbus`).
    `pattern` is the shape every id of the package has, its group `suffix` the multi-inverter variant's `_inv_<n>`
    (empty for the single-inverter file); two inverters' entities are told apart by that suffix and by nothing else."""

    platforms: tuple[str, ...]
    pattern: re.Pattern[str]

    def suffix(self, unique_id: str) -> str | None:
        """The inverter suffix of a package id (`""` for none), `None` for an id that is not the package's."""
        found = self.pattern.fullmatch(unique_id.lower())
        return None if found is None else found.group("suffix") or ""

    def admits(self, platform: str, unique_id: str, suffix: str) -> bool:
        return platform in self.platforms and self.suffix(unique_id) == suffix


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
    #: The `plant_max` entity's attribute that holds it, in W, instead of its state (Solis: `device_limit`).
    plant_max_attribute: str | None = None
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
    #: Set for a YAML package read through a platform other brands share: the adapter is chosen only for a battery
    #: entity whose unique id is the package's, and its entities are looked for by that scope (`PackageScope`).
    package: PackageScope | None = None


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

#: The two integrations that share the domain `solaredge_modbus`, read for the facts only. Not the `SOLAREDGE` adapter
#: (brand `solaredge`): that brand has a control recipe in `commands.py` written against `solaredge_modbus_multi`'s
#: entities, and a control found by key alone on another integration is not a verified one. Its own brand keeps
#: `commands.control_for` empty here.
#: * Core (home-assistant/core `d33dfe3a`, library solaredged 0.5.0): `<inverter serial>_battery_<battery serial or
#:   slot_n>_<key>`; the SunSpec battery block as the Multi's, so the same counters: `energy_imported` (Wh, into the
#:   battery), `energy_exported` (out), `state_of_energy` (%), `energy_max` (Wh, usable; disabled by default),
#:   `max_charge_power` and `max_discharge_power` (W, disabled by default).
#: * binsentsu's HACS integration (`e6734e47`, 2.3.0): `<hub name>_battery<n>_<key>` with a name the person chose;
#:   `state_of_charge` (%), `size_max` (Wh), `energy_charged` and `energy_discharged` (kWh). It states no rating.
SOLAREDGE_MODBUS: Final = Adapter(
    brand="solaredge_modbus",
    label="SolarEdge Modbus",
    platforms=("solaredge_modbus",),
    facts={
        "soc": Match(("sensor",), re.compile(r"(?:_battery_.+_state_of_energy|_battery\d_state_of_charge)$")),
        "capacity": Match(("sensor",), re.compile(r"(?:_battery_.+_energy_max|_battery\d_size_max)$")),
        "rated_charge": Match(("sensor",), re.compile(r"_battery_.+_max_charge_power$")),
        "rated_discharge": Match(("sensor",), re.compile(r"_battery_.+_max_discharge_power$")),
        "charged_total": Match(("sensor",), re.compile(r"(?:_battery_.+_energy_imported|_battery\d_energy_charged)$")),
        "discharged_total": Match(
            ("sensor",), re.compile(r"(?:_battery_.+_energy_exported|_battery\d_energy_discharged)$")
        ),
    },
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
    # The inverter's rated output, from the model chosen in the integration's setup: the advisory `device_limit` (W)
    # of the battery discharge limit power number (43131, `sensors/solis_number_sensor.py` lines 89-102 and
    # `solis_base_sensor.py` `_inverter_rating_max`, v4.5.0). The BMS's current limit can read far above it (a cold
    # Dyness at 100 A x 367 V is 37 kW beside a 15 kW inverter). Absent with "essential sensors only".
    plant_max=_key("solis_modbus_inverter_battery_discharge_limit_power", "number"),
    plant_max_attribute="device_limit",
)

def _any_key(*keys: str, domains: tuple[str, ...] = ("sensor",)) -> Match:
    """One of `keys` at the end of the unique id, after `_` or at its start (Marstek's two integrations)."""
    return Match(domains, re.compile(rf"(?:^|_)(?:{'|'.join(re.escape(key) for key in keys)})$"))


#: Marstek through `marstek_local_api` or `marstek_modbus` (see the module): read only.
MARSTEK: Final = Adapter(
    brand="marstek",
    label="Marstek",
    platforms=("marstek_local_api", "marstek_modbus"),
    facts={
        # Found on the battery entity's own device: the system device's weighted level, else the one battery's.
        "soc": _any_key("system_average_soc", "battery_soc"),
        "capacity": _any_key("system_total_rated_capacity", "battery_rated_capacity", "battery_total_energy"),
        "firmware": _key("firmware_version"),
        "charged_total": _key("total_charging_energy"),
        "discharged_total": _key("total_discharging_energy"),
    },
    modes=(
        ModeRule(
            _any_key("operating_mode", "user_work_mode", domains=("sensor", "select")),
            frozenset({"Auto", "anti_feed"}),
            "operating mode",
        ),
        ModeRule(_key("rs485_control_mode", "switch"), frozenset({"off"}), "RS485 control", required=False),
    ),
    reserves=(ReserveSource(_key("discharging_cutoff_capacity", "number")),),
)

#: EcoFlow PowerOcean through `ef_powerocean_tcpmodbus` or `ecoflow_energy` (see the module): read only. The
#: `ecoflow_energy` keys follow the device's serial, which has no underscore, so `unit_` and `inverter_<n>_` are not them.
_ECOFLOW_SN: Final = r"^[^_]+_"
ECOFLOW: Final = Adapter(
    brand="ecoflow",
    label="EcoFlow PowerOcean",
    platforms=("ef_powerocean_tcpmodbus", "ecoflow_energy"),
    facts={
        "soc": Match(
            ("sensor",), re.compile(rf"(?:^|_)battery_soc$|{_ECOFLOW_SN}soc_pct$")
        ),
        "capacity": _key("battery_capacity"),
        "charged_total": Match(
            ("sensor",), re.compile(rf"(?:^|_)bat_charged_total$|{_ECOFLOW_SN}batt_charge_energy_kwh$")
        ),
        "discharged_total": Match(
            ("sensor",), re.compile(rf"(?:^|_)bat_discharged_total$|{_ECOFLOW_SN}batt_discharge_energy_kwh$")
        ),
    },
    modes=(
        ModeRule(
            _any_key("operating_mode", "work_mode", domains=("sensor", "select")),
            frozenset({"self_consumption", "self_use", "self_powered"}),
            "operating mode",
        ),
        ModeRule(
            _key("modbus_control", "binary_sensor", "switch"), frozenset({"off"}), "Modbus control", required=False
        ),
    ),
    reserves=(ReserveSource(_key("battery_reserve_effective")),),
)

#: Kostal Plenticore through core `kostal_plenticore` or HACS `kostal_modbus_control` (see the module): read only.
KOSTAL: Final = Adapter(
    brand="kostal",
    label="Kostal Plenticore",
    platforms=("kostal_plenticore", "kostal_modbus_control"),
    facts={
        "soc": Match(("sensor",), re.compile(r"[_:]battery_soc$")),
        "capacity": _key("battery_work_capacity"),
        "firmware": _key("software_version"),
    },
    modes=(
        # Core's select reads "None" when the inverter's own smart or timed control is off, which is no reading to
        # SpotNav: core alone is "unknown". The Modbus integration's mode sensor says it (disabled by default).
        ModeRule(
            Match(("sensor", "select"), re.compile(r"(?:^|_)(?:battery_mgmt_mode|devices:local)$")),
            frozenset({"No external battery management"}),
            "battery management",
        ),
    ),
    reserves=(ReserveSource(Match(("number",), re.compile(r"_devices:local_battery:minsoc$"))),),
)

def _fronius(key: str, *domains: str) -> Match:
    """`<id>-<key>` or `<id>-modbus-<key>`: the key after the last hyphen."""
    return Match(domains or ("sensor",), re.compile(rf"-{re.escape(key)}$"))


#: Fronius through core `fronius` (see the module): read only. Its unique ids join the parts with a hyphen.
FRONIUS: Final = Adapter(
    brand="fronius",
    label="Fronius",
    platforms=("fronius",),
    facts={
        "soc": _fronius("state_of_charge"),
        "capacity": _fronius("capacity_designed"),
        "rated_charge": _fronius("battery_power_limit_reference"),
        "rated_discharge": _fronius("battery_power_limit_reference"),
        "charged_total": _fronius("storage_energy_charged_total"),
        "discharged_total": _fronius("storage_energy_discharged_total"),
    },
    modes=(
        ModeRule(
            _fronius("battery_charge_power_limit_enabled", "switch"), frozenset({"off"}), "charge limit", required=False
        ),
        ModeRule(
            _fronius("battery_discharge_power_limit_enabled", "switch"),
            frozenset({"off"}),
            "discharge limit",
            required=False,
        ),
    ),
    reserves=(ReserveSource(_fronius("battery_minimum_reserve", "number")),),
)

#: Homevolt through core `homevolt` (see the module): read only. A sensor's key is a display key with spaces.
HOMEVOLT: Final = Adapter(
    brand="homevolt",
    label="Homevolt",
    platforms=("homevolt",),
    facts={"soc": Match(("sensor",), re.compile(r"^[0-9a-f]{12}_state of charge$"))},
    modes=(
        # Off: Tibber's cloud schedules run the battery (and replace any local one).
        ModeRule(Match(("switch",), re.compile(r"_local_mode$")), frozenset({"on"}), "local mode"),
    ),
)

#: Deye and Sunsynk hybrids through `solarman` (the Deye profiles) or kellerza's MQTT add-on (see the module): read
#: only. Both platforms are shared with other devices, so only these makers' devices are read.
DEYE: Final = Adapter(
    brand="deye",
    label="Deye / Sunsynk",
    platforms=("solarman", "mqtt"),
    manufacturer=("deye", "sunsynk"),
    facts={
        "soc": _any_key("battery_sensor", "battery_soc"),
        "capacity": _key("battery_capacity_sensor"),
        "charged_total": _any_key("total_battery_charge_sensor", "total_battery_charge"),
        "discharged_total": _any_key("total_battery_discharge_sensor", "total_battery_discharge"),
    },
    modes=(
        # The inverter's own programmes: "Disabled" (solarman's select) or off (kellerza's switch) is without them.
        ModeRule(
            _any_key(
                "time_of_use_select", "use_timer", "prog_time_of_use_enabled", domains=("select", "switch")
            ),
            frozenset({"Disabled", "off"}),
            "time of use",
        ),
    ),
)

#: Anker SOLIX through `anker_solix_official` (see the module): read only.
ANKER: Final = Adapter(
    brand="anker",
    label="Anker SOLIX",
    platforms=("anker_solix_official",),
    facts={
        "soc": _key("battery_soc"),
        "capacity": _key("rated_energy"),
        "firmware": _key("device_sw_version"),
    },
    modes=(ModeRule(_key("operating_mode", "select"), frozenset({"self_consumption"}), "operating mode"),),
    reserves=(ReserveSource(_key("discharge_limit_soc", "number")),),
)

#: Zendure through `zendure_ha` (see the module): read only. The ids are built from the device's name in the app, so
#: every match is a key at the end, found on the battery's own device first.
ZENDURE: Final = Adapter(
    brand="zendure",
    label="Zendure",
    platforms=("zendure_ha",),
    facts={
        "soc": _key("electric_level"),
        "capacity": _key("total_kwh"),
        "rated_discharge": _key("inverse_max_power"),
        "charged_total": _key("aggr_charge"),
        "discharged_total": _key("aggr_discharge"),
    },
    modes=(
        # The integration's own manager, a device of its own, writes the limits while it is not off.
        ModeRule(_key("zendure_manager_operation", "select"), frozenset({"off"}), "Zendure Manager"),
        ModeRule(_key("hems_state", "binary_sensor"), frozenset({"off"}), "Zendure HEMS", required=False),
        # 1 while the device calibrates its level and ignores every setpoint.
        ModeRule(_key("soc_status"), frozenset({"0"}), "SoC calibration", required=False),
    ),
    reserves=(ReserveSource(_key("min_soc", "number")),),
)

def _package(key: str, *domains: str) -> Match:
    """A package entity: the key is the whole unique id, with the multi-inverter `_inv_<n>` suffix allowed."""
    return Match(domains or ("sensor",), re.compile(rf"^{re.escape(key)}(?:_inv_\d+)?$"))


#: Sungrow through mkaiser's YAML Modbus package (see the module): read only.
SUNGROW: Final = Adapter(
    brand="sungrow",
    label="Sungrow",
    platforms=("modbus",),
    package=PackageScope(("modbus", "template"), re.compile(r"(?:sg|uid)_.+?(?P<suffix>_inv_\d+)?")),
    facts={
        # 13022, "0..100 % | min_soc..max_soc": the level inside the SoC window or the absolute one is not settled.
        "soc": _package("sg_battery_level"),
        "capacity": _package("uid_battery_capacity_high_precision"),
        "rated_charge": _package("sg_bdc_rated_power"),
        "rated_discharge": _package("sg_bdc_rated_power"),
        "charged_total": _package("sg_total_battery_charge"),
        "discharged_total": _package("sg_total_battery_discharge"),
        "firmware": _package("sg_inverter_firmware_version"),
    },
    modes=(
        ModeRule(_package("uid_ems_mode", "select"), frozenset({"Self-consumption mode (default)"}), "EMS mode"),
        ModeRule(_package("uid_battery_forced_charge_discharge", "select"), frozenset({"Stop (default)"}), "forced command"),
    ),
)

#: Omnibattery (see the module): read only, the system's aggregate entities. It is the outside controller itself, so no
#: mode is read and `outside_control` marks the battery as run by someone else by its presence.
OMNIBATTERY: Final = Adapter(
    brand="omnibattery",
    label="Omnibattery",
    platforms=("omnibattery",),
    facts={
        "soc": _key("system_soc"),
        "capacity": _key("system_total_energy"),
    },
)

#: sonnenBatterie through `sonnenbatterie` (see the module): read only. The sensors' unique ids begin `sensor.`.
SONNEN: Final = Adapter(
    brand="sonnen",
    label="sonnenBatterie",
    platforms=("sonnenbatterie",),
    facts={
        "soc": _key("state_charge_user"),
        "capacity": _key("state_total_capacity_real"),
        # One figure for both directions, anchored on the serial: `tou_max_power` is a schedule setting.
        "rated_charge": Match(("sensor",), re.compile(r"^sensor\.sonnenbatterie_[^_]+_max_power$")),
        "rated_discharge": Match(("sensor",), re.compile(r"^sensor\.sonnenbatterie_[^_]+_max_power$")),
    },
    modes=(
        # Free in automatic (self-consumption); manual is a setpoint someone else sends, time-of-use the person's
        # schedule and optimization sonnen's own (a VPP among them). An unknown mode (a newer firmware's) reads unknown.
        ModeRule(_key("operating_mode", "sensor"), frozenset({"2"}), "operating mode"),
    ),
    reserves=(ReserveSource(_key("battery_reserve", "number")), ReserveSource(_key("backup_buffer"))),
)

#: Alpha ESS SMILE through `alphaess_modbus` (see the module): read only.
ALPHAESS: Final = Adapter(
    brand="alphaess",
    label="Alpha ESS",
    platforms=("alphaess_modbus",),
    facts={
        "soc": _key("soc_battery"),
        "capacity": _key("battery_capacity_kwh"),
        "firmware": _key("ems_version"),
        "charged_total": _key("total_energy_charge_battery"),
        "discharged_total": _key("total_energy_discharge_battery"),
    },
    modes=(
        # 1 while any dispatch runs, SpotNav's own included: a battery SpotNav is not writing is someone else's then.
        ModeRule(_key("dispatch_start", "sensor"), frozenset({"0"}), "dispatch"),
        # The app's windows (0 off, 1 grid charging, 2 discharge control, 3 both); the select of the same key is not it.
        ModeRule(_key("charging_time_period_control", "sensor"), frozenset({"0"}), "time period control", required=False),
    ),
    reserves=(ReserveSource(_key("discharging_cutoff_soc", "number")), ReserveSource(_key("discharging_cutoff_soc"))),
)

ADAPTERS: Final[tuple[Adapter, ...]] = (
    SIGENERGY, HUAWEI, SOLAREDGE, SOLAREDGE_MODBUS, SOLAX, SOFAR, GROWATT, SOLINTEG, VICTRON, VICTRON_MODBUS, GOODWE, TESLA, ENPHASE,
    FERROAMP, FOXESS, SOLIS, MARSTEK, ECOFLOW, KOSTAL, FRONIUS, HOMEVOLT, DEYE, ANKER, ZENDURE, SUNGROW, OMNIBATTERY,
    SONNEN, ALPHAESS,
)


def adapter_for(platform: str, manufacturer: str | None = None, unique_id: str | None = None) -> Adapter | None:
    """The adapter of an integration, `None` for a brand SpotNav has none for. An adapter of a YAML package is chosen
    only for a battery entity whose unique id is the package's (`unique_id`), as `modbus` is every device's."""
    for adapter in ADAPTERS:
        if platform not in adapter.platforms:
            continue
        if adapter.package is not None and (unique_id is None or adapter.package.suffix(unique_id) is None):
            continue
        wanted = (adapter.manufacturer,) if isinstance(adapter.manufacturer, str) else adapter.manufacturer
        if wanted is not None and not any(part in (manufacturer or "").lower() for part in wanted):
            continue
        return adapter
    return None


def package_of(platform: str, unique_id: str) -> tuple[PackageScope, str] | None:
    """The package scope a battery entity belongs to and its inverter suffix, `None` for an entity of an integration."""
    adapter = adapter_for(platform, None, unique_id)
    if adapter is None or adapter.package is None:
        return None
    suffix = adapter.package.suffix(unique_id)
    return None if suffix is None else (adapter.package, suffix)
