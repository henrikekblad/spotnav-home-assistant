---
title: Home batteries
description: SpotNav's battery control (opt-in, in testing), the grid-charge signal for other planners, and export at negative prices.
order: 30
section: House and battery
---

# Home batteries

For anyone with a home battery and a planner such as Predbat or EMHASS: how SpotNav signals a grid charge, why it does
not control the battery by default, and the battery control that is off by default and still in testing.

SpotNav tells your home battery planner when the car is charging from the grid. By default it does not control
the battery itself. SpotNav 3.0 also plans the battery in shadow, as a benchmark you can compare with what the battery really
does; that part only watches and is described in [The battery plan](battery-plan.md) and the
[house card](house-card.md). SpotNav 3.0 also has its own battery control, **off by default and still in testing**:
see [SpotNav's own battery control](#spotnavs-own-battery-control-off-by-default-in-testing).

## Why SpotNav does not write to batteries by default

Battery integrations differ in which settings they expose, and firmware does not always obey them.
On some Sigenergy systems, for example, the discharge limit is ignored in AI mode, and a "hold"
done through the discharge cut-off SoC can make the inverter import several kW from the grid, next
to your main fuse. A planner such as Predbat or EMHASS already owns the battery, and two writers on
one battery fight each other. So SpotNav only publishes a signal, and the planner or an automation of
yours acts on it.

One limit to know before any controller (SpotNav's own or yours) writes to a Sigenergy plant:
Sigenergy has no dead-man. If Home Assistant stops (not restarts) during a command, the plant keeps that command
until Home Assistant is back and the controller restores what it found (SpotNav does that first after a
start).

## SpotNav's own battery control (off by default, in testing)

SpotNav can run the battery itself: hold it while the car charges from the grid, charge it in cheap hours, and
stop export when selling costs money. **It is off by default.** What happens when you switch it on depends on
whether the brand's recipe has been tested on a real plant: for a tested recipe the check below runs and control
starts once it passed; no recipe is marked tested yet (Sigenergy will be once its on-site tests are rerun), so for
every brand it only logs what it would do (the line under the switch says "Test: SpotNav controls nothing
yet and only logs what it would do"), unless the installation is a
[beta tester](beta-testing.md), when the check runs for them too. Every brand but Sigenergy also waits for a passed
hardware test and a control code ([Becoming a tester](#becoming-a-tester)). Either way control runs only once
the brand's check passed, and every fuse rule below applies (beta or not). The switch is in the
settings popover's **Home battery** section, shown when SpotNav finds a battery: from the house card's gear, the heat
card's gear or the charging card's **Settings** (administrators only). The section also has:

* one line under the switch saying what control does now (off, test mode, checking, waiting for a suitable moment and
  why, on, stopped, or only watching while the AI mode, or a battery's own strategy SpotNav cannot read, is not yet accepted);
* the checklist of what you still have to do (enable an entity, turn on the firmware sensor, accept the AI-mode
  takeover or, for a Ferroamp hub, that SpotNav overrides its own strategy, or, for a brand that waits for one, run
  the hardware test first), each in plain words;
* the check's progress while it runs, and its result per step (hold, charge from the grid, cover the house, hand back:
  passed, failed or not judged yet) with what SpotNav may then do;
* the reminder to turn AI mode on again after a takeover ended;
* a line when the battery's cells are cold and its own preheating is off (see **Cold battery** below);
* **Mode**: **Self-consumption**, **Balanced** (the default) or **Max profit**
  ([the three modes](battery-plan.md#the-three-modes)). **Max profit** lets control sell from the battery to the
  grid whenever that pays, **Balanced** only surplus and only when it is highly profitable, **Self-consumption**
  never.

![The Home battery section of the settings: the mode, Battery control, its checklist and the check's steps](images/settings-home-battery.png)

**Sigenergy** (the Sigenergy Local Modbus integration) is the first brand. Before control can run:

* The integration must allow writes (its own option; it is read-only by default and then drops every write
  without an error, which SpotNav notices: every write is read back).
* The plant's Remote EMS switch, Remote EMS control mode, charge and discharge limits and plant maximum power must be
  enabled in Home Assistant (they are disabled by default); for the export part also Grid Export Limitation. You do not
  have to do it by hand: see "Turning on the control entities" below.
* No other controller may run the battery (Predbat, EMHASS, CheckWatt, a VPP in Remote EMS): SpotNav then only
  watches (see below).

**Turning on the control entities.** Some integrations ship the entities SpotNav's control writes disabled (the
Sigenergy one does). When you turn **Battery control** on and any of them are disabled, the dialog says "SpotNav needs
to turn on N control entities in <integration>" with the list folded, and a button that turns them on and control with
them. Only your confirm does it: SpotNav clears the entities' "disabled" mark in Home Assistant's entity registry (exactly
those, nothing else) and reloads the integration once, so it restarts for a moment; the check then runs as usual. They stay
on if you turn control off later. The same button is in the checklist's step for the disabled controls. Only
administrators can confirm, and never over the webhook. The entities per brand:

* Sigenergy: Remote EMS (controlled by Home Assistant), Remote EMS control mode, ESS max charging limit, ESS max
  discharging limit, Grid import limitation (read by SpotNav), and for the per-phase control the Independent phase power
  control switch and the Phase A, B and C active power fixed targets. The Grid export limitation stays off: it arms the
  export curtail.
* SolarEdge (the Modbus Multi integration): storage command timeout, charge limit, discharge limit, command mode,
  control mode and AC charge policy.
* Huawei: forcible charge/discharge write, maximum charging and discharging power.
* SolaX: remote control autorepeat duration, active power, power control and trigger.
* GoodWe (the HACS integration): operation mode, eco mode power, eco mode SoC and the depth of discharge.
* Tesla Powerwall: backup reserve, the grid charging switch and the battery's size (its pack energy).
* Solinteg (the Solinteg plugin of SolaX Modbus): Working Mode and EMS BattCtrl Charge Discharge Power Target, and for
  the per-phase control EMS ACCtrl AC Power Scheduling Mode, Grid Unbalanced Output (read by SpotNav) and the EMS
  ACCtrl Phase A, B and C Power Scheduling numbers (those three are disabled by default).
* Marstek (the Marstek Venus Modbus integration; all four are disabled by default): Max Discharge Power, RS485 Control
  Mode, Set Charge Power and Force Mode.
* Kostal Plenticore (Kostal Plenticore Battery Control): Battery Management Mode (disabled by default), Block
  Discharge, Set Charge Rate and Charge Start.
* Ferroamp: nothing ships disabled; SpotNav reads the integration's "last request" sensor and calls its charge,
  discharge and autocharge services.
* FoxESS (FoxESS Modbus; both enabled by default): Max Charge Current and Max Discharge Current.
* Deye / Sunsynk (Solarman: Battery Max Charging Current and Battery Max Discharging Current; the Sunsynk add-on:
  Battery Max Charge current and Battery Max Discharge current, which the add-on publishes only once its sensor group
  "battery" or "advanced" is in its SENSORS option).
* Victron (the core Victron GX integration and the HACS Victron MQTT; all enabled by default): ESS max charge power
  limit, ESS max inverter power limit and AC power setpoint, and for the per-phase control ESS mode (Hub4), Hub4
  disable charge, Hub4 disable feed-in, AC power setpoint L1, L2 and L3, and the Multi's Input power and Output
  power L1, L2 and L3.
* EcoFlow PowerOcean (the HACS EF-PowerOcean-TcpModbus integration; all enabled by default): Modbus Control, Battery
  Mode, and the Charge Battery and Discharge Battery power.

**GoodWe and Tesla Powerwall: mapped, never run on a device.** Both are switched on by the same switch and checked by
the same routine as the others, and both are untested: nothing is written until a hardware test has passed on the
plant and its result was approved with a control code ([Becoming a tester](#becoming-a-tester)).

**SolarEdge, Huawei and SolaX: mapped, not yet tested on a plant.** The same holds for these three: the hardware test
and a control code come first ([Becoming a tester](#becoming-a-tester)). SolarEdge runs only in Remote Control with
its storage command timeout (30 minutes, renewed); Huawei through the forcible charge services and the battery's
power limits (installer password); SolaX through the integration's remote control, renewed by its autorepeat.

* *GoodWe* needs the HACS integration (mletenay/home-assistant-goodwe-inverter): core's `goodwe` has no eco charge mode
  and no eco power or SoC numbers, and the checklist then says "needs the HACS integration". A grid charge is the eco
  charge mode at a share of the battery's rated power (an estimate from the battery's present current limit and
  voltage, so a charge that takes more from the grid than it was given for a minute is handed back and not tried again
  for six hours); a discharge for the house is the general mode; a hold sets the depth of discharge so the cut-off is
  the level now (not confirmed on a device: the library does not say what the inverter does at that point, so a hold
  whose battery charges from the grid for a minute is handed back and not tried again for six hours). While SpotNav
  holds, the plan still counts the depth of discharge you set, not SpotNav's. Hand-back puts the mode, the eco values
  and the depth back as found. Nothing stops a command when Home Assistant stops, so a grid charge is capped for that
  case. Some firmwares ignore the eco level ceiling (the HACS integration's issues 466 and 486), so a charge ends with
  the plan's slot and is judged by the battery's power, not by the level.
* *Tesla Powerwall* (`tesla_fleet` or `teslemetry`, Tesla's cloud) is **slow**: only the plan's own actions, no hold
  for a car, no peak cover, at most 24 writes a day (Tesla's real limit for energy settings is not confirmed) and no
  change of action within 15 minutes. It runs only while the operation mode is self-powered; Time-Based Control and
  Backup are left alone.
  * A **hold** turns grid charging off and sets the backup reserve two points under the level now, rounded down, so a
    level the cloud reports late never puts the reserve above the battery, where it would charge from the grid. A hold
    that charges from the grid anyway is handed back and not tried again for six hours. While SpotNav holds, the plan
    still counts the reserve you set, not SpotNav's.
  * A **grid charge** allows grid charging and sets the reserve to what the Powerwalls reach in 30 minutes at 5 kW
    each, raised again every 30 minutes. Tesla has **no dead-man**: if Home Assistant or the cloud stops, at most that
    much is charged from the grid and the reserve stays there until it is set back (see below), never at 100 %.
  * The charge power is the Powerwall's own (about 1.7 kW each in reports, not measured here), so SpotNav counts 5 kW
    per Powerwall on one phase for the fuse cap and does not start a charge that does not fit. It does not start one
    at all while the battery's size is not read (the size sensor is disabled by default; the switch's dialog offers
    it): without it two Powerwalls would be checked as one.
  * The grid charging switch exists only on a site with solar. Without it a reserve above the level charges from the
    grid whatever SpotNav does, so there a hold runs only where the Powerwall's charge would fit under the fuse cap.
  * The cloud may take minutes to show a change, and Home Assistant shows SpotNav's write at once. For 15 minutes after
    a hand-back SpotNav watches that the reserve and the switch stay as it set them back; one that shows SpotNav's
    value again is written again twice, and then the switch's line says the control is blocked until the reserve is
    set back. A hand-back the cloud refuses outright is tried again every hour.
  * The **hardware test** waits for all of that at each step (the 15-minute re-arm after each hand-back and up to
    7 minutes per write for the cloud), so on a Powerwall it takes half an hour to an hour, at most 75 minutes, and
    no car charge starts on the site meanwhile. Where the Powerwall's own charge power does not fit under the fuse
    cap, the test skips its charge step (it cannot charge at a smaller power) and can still pass; control then charges
    from the grid only once SpotNav's own check has seen a charge that fits.

**If GoodWe or Tesla Powerwall control goes wrong, restore by hand** (note the values before you turn control on):

* GoodWe: set the operation mode back to General (or your own mode), the depth of discharge (on grid) to your value
  and the eco mode power and SoC to what they were; then turn SpotNav's switch off.
* Tesla Powerwall, in the Tesla app or Home Assistant: set the backup reserve back to your value and grid charging
  (the "allow charging from grid" switch, on a site with solar) back as it was; then turn SpotNav's switch off.

**Ferroamp (EnergyHub), untested.** Nothing is written until a hardware test has passed on the hub and its result was
approved with a control code ([Becoming a tester](#becoming-a-tester)). Control goes through the `ferroamp` integration's services (Ferroamp's MQTT API,
which Ferroamp's support enables): a charge or discharge request with a power in watts for the whole system (steps of
500 W), and `autocharge`, which hands the batteries back to the hub. A hold is a charge request of 0 W (the check also
tries a discharge request of 0 W; neither is documented by Ferroamp, so the hub may refuse it), covering the house is
`auto`, and every hand-back is `auto`. Things to know: the API has no timeout for a request, so a command stays if
Home Assistant stops, and SpotNav caps a grid charge for that case as for Sigenergy; the integration shows the
last request seen on the bus and, beside it, the hub's answer to it: SpotNav counts a request as taken only once the
hub has acknowledged it, and sends a refused hand-back again (the battery's power is still judged as for every
brand); the hub takes one request at a time and refuses a second while one is running; and SpotNav does not start while another request
(Ferroamp's own app, an automation) is standing. The strategy set for the hub in EnergyCloud (peak shaving, FerroAI,
a grid service) is not shown in Home Assistant, so SpotNav cannot tell whether one runs: the checklist asks you to
accept that SpotNav's requests override it while SpotNav controls the battery (every hand-back gives the hub back to
it), and nothing is sent before you do. Leave control off if the hub takes part in a grid service. The hub's ACE moves energy between the phases to keep each grid
phase under the threshold set in EnergyCloud (set it at the main fuse). It runs inside the hub, but Ferroamp documents neither whether it acts on a grid charge nor how the hub spreads a
charge over its three phases, so SpotNav's cap for a grid charge counts it all on one phase, as for a brand that
does not say, and does not depend on ACE.

**Read only: Sofar and Growatt.** SpotNav reads these two through their plugins of the SolaX Modbus
integration (level and power, and what each states of its size and floor: see
[The battery plan](battery-plan.md#what-the-plan-needs)), so the site can tell the car's draw from the battery's and the
battery plan can run in shadow. It has no control for them and writes nothing to them. Both are read from the
integration's source and not yet tested on a battery: [tell us](supported.md) how yours reads.

**FoxESS (H1, KH, H3, H3-Pro, H3-Smart, AC1, AC3; also the 1KOMMA5 `1K5-HI` units): limits only, never run on a
device.** SpotNav reads these through the FoxESS Modbus integration (`foxess_modbus`): the battery's power and level,
its floor (*Min SoC (On Grid)*) and, on the three-phase H3 family, the grid per phase for the site's meter. The H3-Pro
and H3-Smart (and 1KOMMA5's units, which the integration reads as an H3-Smart) report a level per battery pack and none
for the whole battery: SpotNav combines the packs, weighted by the energy each holds. The size is learnt; the ratings
are learnt too, except on the H1 and KH once you enable their two sensors *BMS Charge Rate* and *BMS Discharge Rate*
(disabled in Home Assistant by default).

Its control only lowers the battery's own current limits, read from the integration's source:

* **Hold:** *Max Discharge Current* to 0 A. The battery covers nothing; a sun surplus is still stored. When the house
  needs the battery to keep under the fuse, the limit is given back for that time.
* **Cover the house:** *Max Charge Current* to 0 A. The battery covers the house and stores nothing (a sun surplus is
  exported).
* **Hand back:** both limits to the values they had.
* **No grid charge and no sale.** The integration's only way to charge from the grid is its remote control (*Force
  Charge* in the work mode). Before it starts that, it sets the work mode to *Back-up*, and the inverter runs Back-up
  once Home Assistant stops renewing the command (after 20 to 30 seconds). Back-up charges at full power, and the
  integration does not set the mode back after a restart. A Home Assistant that stopped mid-charge could leave the plant
  charging at full power from the grid, so SpotNav does not use it until a plant has shown what happens. The plan
  therefore plans no grid charge on FoxESS.

What to know before you turn it on:

* **Run the hardware test first.** Nothing is written until a hardware test (Settings → Support → Hardware test) has
  passed once on this inverter and its result was approved with a control code (see
  [Becoming a tester](#becoming-a-tester)); until then control only logs what it would do,
  whatever the test-mode setting says, and the checklist says so. After a firmware or integration update SpotNav checks the battery again by itself, and control runs on meanwhile; only
  if that check fails does the checklist ask for the hardware test again (turning SpotNav's switch off and on also
  lets the check run again, as for every brand).
* **Only in Self Use.** SpotNav takes the battery only while its work mode reads *Self Use*. In *Feed-in First*,
  *Back-up*, *Peak Shaving* or the integration's *Force Charge* and *Force Discharge* it only watches.
* **If Home Assistant stops**, a limit SpotNav set stays: the battery then covers less of the house until Home Assistant
  is back and hands it back. It never takes more from the grid because of it.
* **Writes a day:** 96. Nobody states whether the inverter keeps these limits in flash.
* **Restore by hand:** set *Max Charge Current* and *Max Discharge Current* back to your values (often the battery's
  maximum).

Please [tell us](supported.md) what the hardware test says on yours. To help decide about a grid charge, a supervised
script (`tools/foxess_watchdog_test.py` in the repository) shows what your inverter does when the remote control stops
being renewed, with the charge current capped low first; ask before you run it.

**Deye, Sunsynk and Sol-Ark hybrids: limits only, never run on a device.** SpotNav reads these through the Solarman
integration (`solarman`, davidrapan/ha-solarman, the Deye profiles) or the Sunsynk add-on (kellerza/sunsynk) over
MQTT, only on a device that names Deye or Sunsynk as its maker (see [supported](supported.md)). Its control only
lowers the battery's own current limits:

* **Hold:** *Battery Max Discharging Current* to 0 A. The battery covers nothing; a sun surplus is still stored. When
  the house needs the battery to keep under the fuse, the limit is given back for that time.
* **Cover the house:** *Battery Max Charging Current* to 0 A. The battery covers the house and stores nothing (a sun
  surplus is exported, or curtailed in a zero-export work mode).
* **Hand back:** both limits to the values they had. A limit that someone else changed meanwhile (the Deye or Sunsynk
  app, Predbat, evcc), also while Home Assistant was down, is left as they set it, and control stops (the line under
  the switch says why); after a restart SpotNav waits for the integration's first reading before it writes a limit
  back.
* **No grid charge and no sale.** The inverter has no power setpoint and nothing that ends a command by itself: a
  grid charge is one of the six time-of-use programmes (its time, power, SoC and charge source), and SpotNav would
  have to rewrite the programmes you set. It does not. The plan therefore plans no grid charge on a Deye.

What to know before you turn it on:

* **Only with Time of Use off.** With *Time of Use* on (any weekday choice in Solarman, *Use Timer* or *Prog Time Of
  Use Enabled* in the add-on) the inverter runs its own programmes, and SpotNav only watches. SpotNav never writes
  Time of Use, the programmes, the work mode, the grid charge settings or the export and peak shaving settings.
* **Run the hardware test first.** Nothing is written until a hardware test (Settings → Support → Hardware test) has
  passed once on this inverter and its result was approved with a control code (see
  [Becoming a tester](#becoming-a-tester)); until then control only logs what it would
  do, and the checklist says so. The test holds, skips the grid charge, covers the house and hands back.
* **If Home Assistant stops**, a limit SpotNav set stays: the battery then covers less of the house until Home
  Assistant is back and hands it back. It never takes more from the grid because of it.
* **Writes a day:** at most 48, every write counted (putting a limit back too), and only when the plan changes what
  the battery should do (a write of the value the inverter already holds is skipped). A hand-back always runs; once
  the 48 are spent, nothing new starts until the next day. Nobody states how often these settings may be written; owners worry about the
  inverter's flash memory, so SpotNav keeps to half of what it allows other brands' settings.
* **Restore by hand:** set *Battery Max Charging Current* and *Battery Max Discharging Current* back to your values
  (often the battery's maximum).

Not yet seen on a Deye: how soon a limit acts after the write, and whether every model and firmware takes a single
current write from Home Assistant (some settings are taken only as a group from the vendor's app). The hardware test
shows both; please [tell us](supported.md) what it says on yours.

**Solis (hybrid inverters such as the S6-EH3P): control mapped, never run on a plant.** SpotNav reads these through
the Solis Modbus integration (`solis_modbus`): the battery's power (*Battery Power Net*) and level, its floor
(*Overdischarge SOC*, the on-grid one) and the grid meter. The ratings are the BMS's charge and discharge current limits
times the BMS's voltage, an estimate; the size is learnt (the integration's *Battery Rated Capacity* is a setting in Ah,
not the bank the BMS reports). SpotNav counts the battery as running itself only in the *Storage Mode* "Self-Use" with
neither *RC Force Battery Charge* nor *RC Force Battery Discharge* on; with the integration's "essential sensors only"
option those entities are missing and SpotNav cannot tell (and has nothing to control). A time-of-use period switched
on beside "Self-Use" is the inverter's own schedule: SpotNav then only watches. See [Solis](#solis-mapped-not-yet-tested-on-a-plant) below for the control.

**Marstek (Venus E, C, D, A): read through both integrations, control mapped for the Modbus one, never run on a
device.** SpotNav reads these through either the Marstek Local API integration
(`marstek_local_api`, Marstek's own local API, which you switch on in the Marstek app) or the Marstek Venus Modbus
integration (`marstek_modbus`, RS485 or Modbus TCP): the battery's power and level and its size (*Rated capacity*, or
*Battery Total Energy* over Modbus). With several batteries in one Local API entry SpotNav reads them whole, from the
*Marstek System* device (*Total battery power*, *Average state of charge*, *Total rated capacity*). The ratings are
learnt. Over Modbus it also reads the battery's lifetime charge and discharge counters, and its floor from
*Discharging Cutoff Capacity* once you enable it (Venus E v1 and v2). SpotNav counts the battery as running itself
only in the *Operating mode* "Auto" (local API; "AI", "Manual", "Passive" and "UPS" are someone else's), or in the
*User Work Mode* "Self Consumption" with *RS485 Control Mode* off (Modbus). The local API reports the mode every
5 minutes, and the *Marstek System* device has none, so with several batteries in one entry SpotNav cannot tell who
runs them. The official Marstek integration (`marstek`) is not read: it reports the battery's power as a number with
no direction. Read from the integrations' source and not yet tested on a battery: [tell us](supported.md) how yours
reads.

SpotNav's control for Marstek goes through the **Modbus integration only** (the local API has no limit to set, and its
Passive mode, the only command there that ends by itself, is acknowledged and then dropped in field reports; on the
local API the checklist says the Modbus integration is needed). It is behind the same switch and the same check as
the other brands, but **nothing is written until a hardware test (below) has passed** once on the battery and its
result was approved with a control code ([Becoming a tester](#becoming-a-tester)): until
then control only logs what it would do, whatever the test-mode setting says and on a beta tester's installation
too, and the checklist says so. After a firmware or integration update SpotNav checks the battery again by itself,
and control runs on meanwhile; only if that check fails does the checklist ask for the hardware test again.

* A **hold** sets *Max Discharge Power* to 0. The battery stays in its own self-consumption: it still stores a sun
  surplus and covers nothing. This is a limit that is safe if it is left standing. On a Venus A or D with its own
  solar input, a hold at 100 % may also stop its own panels (evcc issue 36206).
* A **grid charge** turns *RS485 Control Mode* on, sets *Set Charge Power* (in steps of 50 W, at most 2500 W, and
  never more than the entity itself takes: 1500 W on a Venus A) and then *Force Mode* to charge. The battery keeps a
  force charge until it is told otherwise: there is **no dead-man**, so if Home Assistant stops, the charge goes on
  until the battery is full. SpotNav therefore caps a grid charge for that case, counting all of it on the one phase
  the battery is plugged into (see the grid charge cap below). *Charge to SoC* is never written.
* A **discharge for the house** is the battery's own self-consumption: nothing is written. Discharging to the grid and
  the battery's *Max Charge Power* are not used.
* Every **hand-back** sets *Force Mode* back to standby, turns *RS485 Control Mode* off (the battery is then in its own
  work mode again) and sets *Max Discharge Power* back to what it was. *Set Charge Power* is left as SpotNav last wrote
  it: it does nothing without a force charge.
* SpotNav does not start while *Force Mode* reads anything but standby, or *RS485 Control Mode* is on (someone else's
  force commands, such as evcc), or the work mode is not "Self Consumption".
* **Batteries run by Omnibattery** (ffunes/Omnibattery) are not this integration's: Omnibattery is the battery's one
  Modbus client and its own controller, so the Modbus integration's entities do not exist. SpotNav reads the
  Omnibattery system and only watches it; it never writes to a battery Omnibattery runs.

**The Marstek hardware test** (Settings → Support → Hardware test in the card, with battery control switched on and
the four entities enabled; at most ten minutes). Press Start: it runs by itself at a suitable moment, and nothing else
is needed from you. If you like to follow it in the Marstek app, the steps and what it shows:

1. **Hold** (about a minute): *Max Discharge Power* reads 0 within some seconds; the battery stops discharging
   (*Battery Power* near 0, or positive if the sun charges it). *RS485 Control Mode* stays off.
2. **Charge from the grid** (a few minutes, at most 2 kW, never more than *Set Charge Power* takes (1500 W on a Venus
   A), and less if the fuse needs it): *RS485 Control Mode* on, *Set Charge Power* at the test's power, *Force Mode*
   charge; *Battery Power* positive at about that power, and the grid import up by about as much on the battery's
   phase.
3. **Cover the house**: *Force Mode* standby, *RS485 Control Mode* off, *Max Discharge Power* back; the battery covers
   the house again.
4. **Hand back**: everything as it was before the test.

The test stops at once and hands back if a write does not read back, if the battery does not do what it was told, or
if a phase comes near the main fuse. If something is left behind (Home Assistant restarted mid-test, say), restore by
hand: *Force Mode* to standby, *RS485 Control Mode* off, *Max Discharge Power* to your value (often 2500 W, or 800 W in
Germany), then turn SpotNav's switch off. If the battery's maximum powers read 800 W after the test, toggle them in the
Marstek app (evcc issue 36110 saw that after its own stop). Send the result: it tells which firmware the control worked
on. The test passes when the hold, the charge and the hand-back passed and nothing failed; only then does SpotNav
write outside the test.

**Solinteg (MHT, M2HT; also M-TEC Energy Butler and Wattsonic): mapped, never run on a device.** SpotNav has the
commands for the Solinteg plugin of the SolaX Modbus integration behind the same switch and the same check, read from
the plugin's source and from owners' notes. Nobody has run them on an inverter yet; the first to do so is a pilot.
Nothing is written until a hardware test has passed on the inverter and its result was approved with a control code
([Becoming a tester](#becoming-a-tester)).
What it does:

* **Hold:** Working Mode *EMS BattCtrl* with the battery's power target at 0. The battery stands idle and stores no
  sun meanwhile (a surplus is exported).
* **Charge from the grid:** *EMS BattCtrl* with the target at minus the power (negative charges). The mode is
  written first and the target after it: owners found that values written before the mode switch read back but do
  not act.
* **Cover the house:** the inverter's own *General* mode. There is no EMS mode that covers the house and stores
  nothing, so the battery also stores a sun surplus then.
* **Hand back:** Working Mode to *General* (one write releases the battery), then the target to 0. SpotNav takes the
  inverter only in *General*, so that is the mode it was found in.

What to know before you turn it on:

* **The inverter keeps the last command.** No timeout is known: if Home Assistant stops during a grid charge, the
  charge goes on until something writes the inverter again. So a grid charge is always capped as described under
  "The grid charge cap and the house's peaks" below, and because nothing says how the inverter spreads such a charge
  over its phases, the cap counts **all of it on one phase**: a third of what a plant known to balance gets. On a
  small main fuse that can leave no grid charge at all until the house's peaks are learned.
* **The inverter's own limits are not counted on.** The site import limit (Import Limit, peak shaving) may or may
  not act under EMS control, and the Solinteg cloud has been seen to write limits back. SpotNav neither writes them
  nor relies on them. The EMS BattCtrl Max Grid Import/Export numbers are left as you have them: if one of them
  stops a charge, the check fails on that step and says so.
* **Nothing left behind.** SpotNav starts nothing while the power target reads anything but 0: it would run for some
  seconds when the mode is switched. Set it to 0 and control goes on.
* **One Modbus client.** The dongle answers one client only: do not run another Modbus tool beside the integration.
  It also stops answering for 4 to 8 seconds about once a minute while it uploads to the cloud. Set the
  integration's timeout to 10 to 12 seconds, so that a write made in such a pause waits instead of failing; a write
  the integration reports as failed stops control until you turn it on again. SpotNav waits up to three minutes
  for a written value to show (one minute on other brands); a hand-back to *General* waits one minute and is then
  written again.
* **The cloud and the app.** If the Solinteg app, the cloud or another controller (Samvolt, CheckWatt, Enequi)
  changes the Working Mode while SpotNav runs it, SpotNav hands back and stops; it never writes over them.
* **Writes a day:** 500 to the power targets, 96 to the modes. Nobody states whether the inverter keeps the targets
  in flash.
* **Restore by hand:** set Working Mode to *General* and the EMS BattCtrl Charge Discharge Power Target (and the
  three phase numbers) to 0.

Please [tell us](supported.md) what the check says on yours.

**Switching on starts a check, not control.** SpotNav first checks that the battery obeys, each step through the
same write path control uses: hold (the battery stops discharging), charge 2 kW from the grid, cover the house, and
hand back (every setting back as found). Each step waits up to 5 minutes for the writes, 5 minutes to settle and
takes three readings a minute apart: about 40 minutes in all. It runs at a suitable moment: the battery between 30
and 80 % and discharging (so a hold can be seen), no car charging, room under the fuse; a weekday evening is
typical. A step the battery cannot show at the moment (nothing to stop, nearly full) is tried again at a later
suitable moment, not counted as a failure; after five runs without an answer the check gives up.

**How SpotNav holds a Sigenergy plant: Command Charging (PV First) with the charge limit at 0.** The protocol's
Appendix 6 names this mode "Command charging (consume PV power first)": a charge at the charge limit, from the sun
first and then from the grid. With the limit at 0 it charges nothing, and no Command Charging mode discharges, so the
battery stands idle and a sun surplus is exported while the hold lasts. It works on both protocol generations
(Sigenergy's Modbus Protocol V2.7 says the charge limit acts only in Command Charging, modes 3 and 4; V2.9, May 2026,
says in every mode). It is also what the plant keeps doing if Home Assistant stops in the middle of a hold, and then
the plant's own grid import limit still covers a peak: tested on site in November 2026 with Home Assistant writing
nothing, the plant started to discharge about 25-30 seconds after the import limit was lowered under the house's
load and held the grid there, the same on every phase. An earlier hold, Maximum Self Consumption with the discharge
limit at 0, failed that test (the grid took the whole house) and is no longer used. Under a peak while Home Assistant
runs, SpotNav switches the mode back to Maximum Self Consumption; a mode change can take the plant a little longer
than a limit (the plant's *active power regulation gradient*, register 40049, sets how fast it ramps; SpotNav only
reads about it, never writes it). If the headroom is not back within 30 seconds the battery is handed back. On every
change between SpotNav's commands the charge and discharge limits are lowered before the mode changes and raised
only after it, so the plant never runs a Command mode on the previous command's limit.

Storing the sun during a hold (the charge limit following the measured surplus, never above the grid charge's
Home-Assistant-down cap) is built but switched off until it has been tested on a sunny day; until then the hold
exports a sun surplus.

The hold is judged on what the battery does while the house draws: it must not discharge and must stand idle. If it
charges more than 0.5 kW for a minute, the plant did not take the limit and the hold is handed back at once, in the
check and in control alike (judged on the battery's own power, so it works without a grid reading). The check then
fails; in control the block clears by itself after six hours. A firmware update runs the check again (control runs on meanwhile). A discharge to
the house stays Maximum Self Consumption with the
charge limit at 0 (V2.7 has no mode for "cover the house, store nothing"; where the limit is ignored the battery
also stores a sun surplus, as it does normally). Other protocol changes worth knowing: V2.8 removed register 41500
(Remote EMS dispatch enable, not used by SpotNav) and V2.9 added V2G (0x08) to the Remote EMS control modes.

**The check decides what SpotNav may do.** Hold and the hand-back must pass for any control; grid charging only if the
charge step passed, covering the house only if that step passed. Selling from the battery needs the house step;
on a battery whose one power target both charges and discharges by its sign (SolaX, Solinteg) it needs the charge
step too, since only that step shows which sign charges. A failed step leaves control off, with one line
saying what did not work (for example "the battery did not stop discharging when held"); turning the switch off and
on again runs the check again. The result is kept per firmware and integration version.

**An update never stops control by itself.** After a firmware update, a new version of the battery's integration or
a changed SpotNav recipe, SpotNav keeps running the battery with what last passed for that brand on your plant: the
same steps stay allowed, nothing more. It checks the battery again at the first suitable moment, at any hour: the
same conditions as the first check (30 to 80 %, discharging, no car charging, room under the fuse), at a time when
the plan leaves the battery to itself. Meanwhile the card says *On: SpotNav keeps running the battery. The check
after the update waits for a suitable moment*, with what it waits for. While the check itself runs (about 40
minutes) control steps aside, as for the first check. Each step counts as soon as it has an answer on the new
version: one that passes stays allowed, one the battery no longer obeys is switched off, and one that could not be
shown yet keeps its earlier answer until a later run. After five runs the answers on the new version stand alone:
steps never shown are then off, while those that passed keep running. If hold or the hand-back fails, or is still not
shown in five runs, control stops exactly as after a failed first check, with the same one line (for a brand that needs the
hardware test: the checklist asks for it again). After such a failure, or a failed hardware test, the earlier result
is no longer used: turning the switch off and on runs the check again and control waits for it. A result is never
carried to another battery brand, and a recipe a release holds back stays off whatever passed before. One thing is
measured again before it counts: where the battery's own timeout lifts the grid charge limit that applies while
Home Assistant is down, that limit is back until the timeout has been measured on the new version.

**What the plan itself shows on the new version counts.** The check does not have to show every step itself. When
the plan holds the battery, charges it from the grid or lets it cover the house on the new version, and the battery
is seen to do it (three readings in a row, as for any command), that step has passed on the new version. A hand-back
that leaves every setting as it was found counts as the hand-back. Only a clear case counts: a hold must stop a
battery that was discharging while the house draws from the grid, a charge or a discharge must be at least 1 kW, and
a charge must start on a battery that was not already charging. A command the battery did not follow is counted as
before but never fails the check: only the check itself does that, and its answer replaces the plan's. The check
then runs only the steps still open. What is still not shown seven days after the update is checked ahead of the
plan: at the next moment that suits the check (the same conditions as above) the plan steps aside for it. Control
never stops because the seven days have passed. The battery's own timeout, a charge per phase and the export limit
are never shown by the plan, only by the check.

The firmware is the integration's firmware entity, or, where the brand has none or it reads nothing, the software version
of the battery's device (SolaX Modbus keeps its firmware registers internal and has only that), so control never waits
on a firmware it cannot read.

**What control does.** It follows the battery plan's running quarter hour, and holds the battery when the car
charges from the grid in a quarter hour the plan leaves to the battery. Every write is read back; every command's
effect is judged; anything unexpected hands the battery back (in Maximum Self Consumption) and stops. Safety rules:

* **The fuse.** A grid charge is clamped to the live headroom under the main fuse. Because Sigenergy has no
  dead-man, it is also capped so that the charge and the house's peak stay under the fuse on every phase even if
  Home Assistant stops while it runs (below; per phase where the plant can, further below). Under a hold the battery is let go at once when a phase nears the fuse
  (a kettle, the oven), and a hold meant to leave the battery idle that charges it anyway is handed back within a
  minute.
* **What backs off first** when the fuse is short: the battery's grid charge, then the car; the car first when its
  departure has room. With the battery above the cars in the site's
  [priority list](site-and-load-balancing.md#the-priority-list) ("battery first") the car backs off before the battery.
* **Selling from the battery** (discharging to the grid) follows the mode: never in **Self-consumption**, only when it
  is highly profitable in **Balanced**, whenever it pays in **Max profit**. It needs the check's house step (above),
  and it is capped so that it and the sun's export keep every phase under the fuse.
* **At most 96 writes a day** to the plant's settings (limits, modes, switches), outside hand-backs. A grid charge
  leaves the last 12 of them untouched, so a hold can still start after a day of charges that kept starting. The
  targets per phase are setpoints made for an outside controller and have their own count, 500 a day on Sigenergy
  (its protocol does not say how often they may be written, so not more) and 3 000 a day on Victron (whose
  setpoints must be written again within a minute anyway; those renewals are not counted); reaching it ends charging
  per phase for the day and nothing else. The diagnostics show what is left of each.

### The grid charge cap and the house's peaks

The cap answers one question: how much may the battery draw from the grid so that no phase passes the main fuse if
Home Assistant stops and the command stays? It is the fuse, less a margin, less what the chargers may draw, less the
house's peak; what is left is spread over the plant's phases. The house's peak is what SpotNav has to learn. While
Home Assistant runs, the charge is also kept under the phase with the least room now, counted the same way: on a
plant not known to balance its charge, all of it on that one phase. On a Marstek a grid charge, planned or running,
is also never more than the battery's own *Set Charge Power* takes (its maximum in Home Assistant).

* **The first week: a cautious rule.** With fewer than seven days learned, the house is taken to draw the house
  forecast's high figure for the hour, at least 4 kW, all of it on one phase (17 A). On a 20 A fuse that leaves no
  grid charge worth running; on 25 A about 3.9 kW. Self-consumption runs as usual meanwhile.
* **From seven days: the learned peaks, with a further margin.** SpotNav keeps each phase's largest one-minute
  current of the last four weeks (the site meter's reading less the chargers' and the battery's share). The cap then
  takes the largest phase's peak as the house on every phase, plus 2 A, plus a further margin: 4 A at seven days,
  falling evenly to nothing at two weeks. So the cap grows day by day, and it replaces the cautious rule in both
  directions: larger for a house that never comes near 4 kW on a phase, smaller for one that passes it. It is never
  taken below the house forecast's high figure for the hour spread over the three phases, so a day colder than the
  days learned still counts.
* **A day counts**, toward the seven days and toward the two weeks alike, only when its minutes are spread over
  the day: in at least twelve of its clock hours (or ten hours of minutes in at least eight). Two quiet hours every night are not a
  day, however many nights. With a Sigenergy plant whose per-phase
  power sensors are enabled (`Plant phase A/B/C active power`, disabled by default in the integration) every minute
  can be reckoned; without them only the minutes where the battery is nearly idle (within 0.5 kW), and what the
  battery hid meanwhile is added to every phase's peak in full.
* **The days before SpotNav.** A few minutes after Home Assistant has started SpotNav reads the same sensors back
  from the recorder (its raw history, ten days by default) and reckons those days by the same rules, so a site whose
  sensors have been in Home Assistant for a while usually has its week at once. The history says when a value last
  changed, not when it was last reported, so a reading that stood still for more than two minutes counts nothing
  there. That makes such days weaker evidence, and they are treated so: a day that lost more than 30 % of its
  minutes that way is no day, nor is one without its evening (minutes reckoned in at least three of the hours
  16 to 22, where a house's peaks are), the two weeks need at least three days sampled live however long the
  history, and a day from the history counts half of a day sampled live when the further margin
  is taken off (ten days of history alone keep the whole 4 A) and toward the two weeks. A sensor that is excluded
  from the recorder has no history: if the site's measurement needs it (on a Sigenergy plant the grid's reactive
  power and the inverter's phase voltages), no earlier day can be reckoned and the learning starts from now; the log
  names the sensors. Long-term statistics are not used: an hour's mean or maximum per sensor is not a current the
  house drew at one moment.

The house card says so while the learning holds the grid charge back ("Grid charging at most 1.4 kW while the house's
peaks are being learned (9 days left)."), and the battery's settings say what the cap rests on. The diagnostics hold
the numbers: the days counted, the days that count in this stage, how many come from the history and how many of
those were left out, the further margin now, and what the cap would be by the cautious rule and by the learned peaks
alone.

### The plant's own import limit is a total

A Sigenergy plant has its own grid import limit (`Grid Import Limitation`, a number in the integration, disabled by
default). On site it was seen to act while SpotNav controls the plant: set to 6 kW it held the import at 6.0 kW
within about five seconds by cutting the battery's charge. But it limits the **total** over the three phases, not
each phase, and the plant spreads its charge evenly. So a limit at the fuse's own power (13.8 kW on 20 A) protects
nothing in a house that loads one phase: with 18 A on L2 alone, the plant sees 4 kW of 13.8, fills the rest with
charge, and a third of that lands on L2: 29 A.

SpotNav therefore lets the limit replace the cap above only when both hold:

* you have told it that the limit acts on your plant (`battery_control_import_limit_acts`, after the on-site test), and
* the limit reads **at most three times the fuse (less the 2 A margin), less twice the largest phase's house peak
  and twice the chargers' last currents**, in amps over the phases. That is the value at which a phase at its peak,
  with the other two quiet, still stays under the fuse when the plant shares out what the limit leaves. The house
  peak is the same as for the cap: the learned one, and 4 kW on one phase until then.

The rule rests on two things about the plant, and is refused where they do not hold:

* **The plant spreads a balanced charge evenly over the three phases.** That is what a three-phase SigenStor is said
  to do, but it has to be read on your plant: during a balanced grid charge (the check's *charge* step), the plant's
  `Phase A/B/C active power` must read about a third of the charge each. Do that before you turn
  `battery_control_import_limit_acts` on (again). A plant that is not three-phase never gets the rule.
* **The plant's limit sees everything that feeds or loads the phases.** With another inverter's sun (a solar source
  in the Energy dashboard that is not the plant's own), or a charger that runs through the plant and can put out as
  well (Sigenergy's DC charger, V2G), the total the plant holds says too little about one phase. The rule is then
  refused whatever the limit reads, and the battery's status says why (`foreign_pv`, `dc_charger`,
  `plant_not_three_phase`).

A Sigenergy DC charger keeps its own cap at the fuse for now, whatever the limit reads: that the limit curtails the
charger when the battery reaches its floor has not been shown on site.

On a 20 A fuse that is 4.4 kW until the peaks are learned, and 4.1 kW for a house whose worst phase peaks at 18 A,
well under the fuse's 13.8 kW. The battery section says what the limit reads and the most it may read
("Anläggningens importgräns står på 13,8 kW. Den begränsar summan över de tre faserna, inte varje fas, och skyddar
säkringen först vid högst 4,4 kW"). **SpotNav never writes the limit**: you set it, in the integration or the
Sigenergy app. While it reads more, or a car charges at a current that leaves it no room, the cap above is in force
as if there were no limit. The same rule is used for any brand whose import limit is a total. A Sigenergy plant
that holds the limit per phase gets the far better rule of the next section instead.

### Grid charging on a small main fuse (Sigenergy)

**Why the cap leaves nothing on 16–20 A.** The cap above is sized for the worst case: Home Assistant stops while a
grid charge runs, the command stays, and the house reaches its peak on one phase. On a 20 A fuse the house's peak on
one phase (4 kW until learned, often 15–18 A once learned) plus the 2 A margin leaves little or nothing, and the
balanced charge takes the same on all three phases. So a small fuse gets no grid charging at all, or a trickle, even
in hours when the house is nearly idle. The total-only rule in the section before does not help either: it asks for
a limit far under the fuse (4.4 kW on 20 A).

**What replaces it.** A Sigenergy plant with *Independent phase power control* on holds each phase's grid import at
a third of its own import limit (`Grid Import Limitation`) by itself, on its own grid sensor, whatever Home
Assistant does. When a load comes on one phase, the plant lowers its charge on that phase (and puts out there if it
must) within seconds and keeps charging on the other two. Sigenergy's AI mode charges from the grid this way every
day. Where SpotNav can rely on that hold it drops the cap: the grid charge runs at the plan's power and the plant
takes what the house leaves. While Home Assistant runs, the live per-phase headroom still keeps the chargers first.

**The test it rests on.** On 2026-11-21 a SigenStor EC 12.0 TP (Remote EMS, control mode *Command Charging (Grid
First)*, the mode SpotNav's grid charge uses, `Independent phase power control` on) held every phase at a third of
its limit within 15 seconds with a single-phase load on: at full charge, with the charge lowered so that the total
stayed **under** the limit (the case that tells a per-phase hold from a total-only one), and after ten minutes with
nothing writing to the plant. That result is recorded in SpotNav's Sigenergy recipe; another brand never gets the
rule.

**Checked at every start and every look.** The cap is dropped only while all of these read so, and comes back at
once when one does not:

* the plant is three-phase, with no other inverter's sun in the Energy dashboard and no Sigenergy DC charger run
  through the plant (as for the total rule);
* you have said the limit acts on your plant (`battery_control_import_limit_acts`, below);
* the plant's `Output Type` reads L1/L2/L3/N (the only type the phase control acts with);
* `Independent phase power control` reads on;
* the plant's `Grid Sensor Status` reads connected (the limit regulates on that sensor);
* the limit reads at most what keeps every phase under the fuse less the site's safety margin.

The house's peak and the chargers' currents do not lower that. On a phase where they draw more than a third of the
limit, the plant charges nothing there (it charges on the other phases and puts out on the loaded one where it can),
so that phase carries what the house and the cars put on it, as it would without a battery. A phase that the house
and the cars alone take past the fuse is the load balancer's matter, and the fuse's while Home Assistant is down, as
on any site.

The limit only guards loads behind the plant's grid sensor. A charger (or any other load) wired upstream of that
sensor, between it and the main fuse, is not seen by the plant, so the plant does not hold it under the limit.

**The site's safety margin sets the most the limit may read:**

    max import limit = 3 x (main fuse - safety margin) x phase voltage

For 20 A at 230 V that is 13.8 kW with a margin of 0 A and 13.1 kW with 1 A (the default for a new site). The margin
is the one you set for load balancing; it covers a voltage dip (the plant holds kW, so a lower voltage means a
little more current) and the plant's own overshoot for a reading or two after a step, both short against what a
fuse carries.

**The risk that is left.** With Home Assistant down nothing re-checks these readings, so a second fault in that
time leaves the charge without this guard: the grid sensor dropping, the limit raised or the phase control turned
off in the app. That is the same exposure Sigenergy's own AI mode runs with every day, and it is accepted here.

**Turning it on, step by step:**

1. In the Sigenergy integration, enable `Grid Import Limitation`, `Grid Sensor Status`, `Output Type` and
   `Independent phase power control` if they are disabled (they are by default). SpotNav only reads the last three.
2. Turn `Independent phase power control` on, and check that `Output Type` reads L1/L2/L3/N.
3. In the card's battery settings, press the button on the import limit's line and confirm:
   * **Set 13.1 kW…** when the plant's limit reads more than 3 x (fuse - safety margin) x voltage, or nothing:
     SpotNav lowers `Grid Import Limitation` to that figure (13.1 kW on 20 A with a 1 A margin), reads it back, and
     only then says the limit acts. It never raises the limit, never writes over a limit that reads nothing, and
     writes nothing to the plant while battery control is off or only watches (shadow): then the line says to set
     it in the Sigenergy app. If the value does not read back within 15 seconds nothing else changes and the line
     says so: set it in the Sigenergy app instead and press again. Many integrations show a written value at once,
     so the plant's own reading at the next poll is the real check; SpotNav checks the limit again at every look.
   * **Turn on…** when the limit already reads low enough: only "The import limit acts" is turned on.

The battery section says what is still missing, one step at a time, with a button to the entity or the setting
("Independent phase power control is off or cannot be read. Turn it on …", "Sigenergy can hold each phase under the
plant's own import limit, but it reads 13.8 kW. At 13.1 kW or less …"). Once everything holds it says so instead of the cap: "Sigenergy holds each phase under 4.4 kW
(the plant's import limit, 13.1 kW, shared over three phases): grid charging uses what the house leaves."

**Another plant.** Owners of another Sigenergy model or firmware who want to see the hold for themselves can run
`tools/sigen_import_limit_test.py` (Python's standard library only, about 45 minutes, someone at the main panel):
it sets a low limit, runs the load steps above with markers and puts every value back afterwards. `--dry-run`
writes nothing.

### Accepting the risk if Home Assistant stops (other brands)

Without the plant's own per-phase hold above, the cap reckons with Home Assistant stopping mid-charge, or losing
contact with the battery or the grid meter (an integration or Modbus link that drops; a meter reading that stands
still is handed back after about two minutes): the battery keeps charging at the power it was last given, and the
house may be at its peak meanwhile. On a 16–20 A fuse that
often leaves almost no grid charge. An administrator can accept that risk instead: in the card's battery settings,
"If Home Assistant stops" is shown only where it would make a real difference (battery control on, a plant that can
charge from the grid, the cap clearly below what the fuse allows with an empty house) and where the plant's own
per-phase hold above is no way: on a Sigenergy plant that can take that way, the import limit's line is shown
instead, since it guards the fuse without accepting a risk. An acceptance given earlier stays shown so it can be
turned off. Tick "I understand and
accept …" in its editor.

With it on, the cap is what the fuse allows with an **empty** house: each phase's fuse less the site's safety margin,
so a command never asks for more than an empty house could take. Everything that works while Home Assistant runs is
unchanged: the phases are read every 10 seconds and the charge is lowered (or handed back) when the house draws
more, the write caps, the read-back, the hand-back and the chargers' load balancing. What you accept: if Home
Assistant stops, or loses contact with the battery or the grid meter, during a grid charge of the home battery and
the house draws a lot before that is over, a phase may go over the main fuse and the fuse may trip. A battery whose
charge SpotNav cannot lower within seconds (Tesla, through its cloud) keeps the cap whatever is accepted. It changes nothing where the plant's own import limit already guards
the fuse. Only an administrator can turn it on or off; the debug bundle shows it as
`battery_control_accept_ha_down`. It is given for the site's main fuse and battery as they are: change either and
it is turned off again, to be given anew. The owner's test action still charges at 2 kW unless a power is given.

### Grid charging per phase

A house that loads one phase leaves the balanced charge nothing: the cap takes the worst phase as the house on every
phase. A plant that takes a power target per phase can use the room the other phases have. SpotNav then gives each
phase its own cap,

    phase cap (kW) = phase voltage x max(0, fuse - 2 A - that phase's house peak - the chargers' last currents) / 1000

and commands the grid charge as three targets, each at most its phase's cap and its phase's live headroom, in whole
amps (rounded down), the sum at most the plan's power and the battery's rating, spread as evenly as the caps allow. The targets stay if Home
Assistant stops, as any command does, so every one of them is sized to keep its own phase under the fuse with the
house at that phase's learned peak. With learned peaks of 13.6 A, 18 A and 17.4 A on a 20 A fuse the balanced charge
gets nothing, and per phase L1 gets 4 A (0.92 kW) and L2 and L3 nothing (L3's 0.6 A is no whole amp). Until the peaks are learned the cautious rule
(4 kW on any one phase) applies to every phase, which leaves nothing on 20 A either way.

**Sigenergy** does this with independent phase power control: in Remote EMS with the control mode *PCS Remote
Control* the plant follows `Phase A/B/C active power fixed adjustment` (negative charges from that phase). For
SpotNav to use it:

* enable the three `Phase A/B/C active power fixed adjustment` numbers, the plant's `Active power fixed adjustment`
  number and the `Independent phase power control` switch in the integration (all disabled by default; the card
  offers to enable them when you turn control on), and the plant's `Phase A/B/C active power` sensors;
* have `Independent phase power control` **on**. SpotNav reads it and never changes it, since it also changes how
  the plant shares its power in its own modes.

The plant's total `Active power fixed adjustment` acts in the same mode, so SpotNav keeps it at 0 while it charges
per phase. And without charging per phase SpotNav starts nothing while that number or a phase's adjustment reads
anything but 0 (where the entities are enabled): turning Remote EMS on passes through PCS Remote Control for some
seconds, and a value left there by someone would run. Set it back to 0 and control goes on.

**It is checked first.** The check gets a further step, *per phase*: every target at 0 for a minute, then on each
phase in turn a discharge of 1 kW for about two minutes (a discharge lowers that phase's import, so it needs no room
under the fuse and can be tried on a phase that has none). Each time the plant's own power on that phase must read
the target and the other two nothing, and the house on that phase, as your meter sees it, must not seem to move:
that shows the plant's phase A really is your meter's L1, and so on. A plant wired otherwise fails the step and
never charges per phase. The step runs by itself at a suitable moment once the entities are there; it does not
affect the rest of control. It is left for a later moment, never failed, while the sun gives more than the house
takes, the battery is near full or near its floor, or the export limit is held at 0.

**When it is used.** Only when the step passed on this firmware, everything it needs is still there, and the
phases' caps allow at least 0.5 kW more in all than the balanced cap. Otherwise the grid charge is the balanced one
under the balanced cap, exactly as before: a balanced house, a plant without the entities, a step not passed, a
reading per phase missing, or the plant's own import limit guarding the fuse (above). While a charge per phase runs
each phase is judged on the plant's own power: a phase that takes more than its target (seen within about a
minute), or a plant that takes nothing, hands the charge back and ends per-phase charging on that firmware. Any phase within 1 A of the fuse hands
the whole charge back at once, whichever phase it is on, so the plant covers that phase again. The battery section
shows it: "Nätladdning per fas: L1 1,01 kW · L2 0 kW · L3 0,13 kW".

Handing back writes the three targets to 0, then the control mode to Maximum Self Consumption, then Remote EMS off,
each read back.

**Solinteg** has the same kind of targets: in Working Mode *EMS ACCtrl* with *EMS ACCtrl AC Power Scheduling Mode*
at "Power Setting on each Phase" the inverter follows *EMS ACCtrl Phase A/B/C Power Scheduling* (the inverter's AC
power on that phase, negative taking from the grid), with *Grid Unbalanced Output* on, which SpotNav reads and never
changes. SpotNav has the commands (the targets, then the scheduling mode, then the working mode; back: *General*
first, then the targets to 0), but **it cannot use them yet**: the check and the watch per phase need the inverter's
own power per phase, and the integration reads only its current per phase without a direction. Until it does, a
Solinteg grid charge is the balanced one under its cap.

### Victron ESS (mapped, not yet tested on a plant)

For a GX device with a MultiPlus or Quattro and the ESS assistant, through the **Victron GX** integration in Home
Assistant core or the HACS **Victron MQTT**. Nobody has run it on a plant yet: it is built from Victron's own page
"ESS mode 2 and 3" and the integrations' source, it sits behind the same switch and the same check, and a
[field report](supported.md) is what it needs. Nothing is written until a hardware test has passed on the plant and
its result was approved with a control code ([Becoming a tester](#becoming-a-tester)). The community Modbus integration (`victron`) is read only: its
maximum inverter power cannot show "no limit" as a value SpotNav could write back. With the HACS integration set
to its read-only operation mode the controls are sensors, and SpotNav finds nothing to write.

SpotNav takes the plant only in an *Optimized* ESS mode with Dynamic ESS off, and uses Victron's two documented
ways in:

* **Mode 2, the GX keeps running its own loop.** A hold sets *ESS max inverter power limit* to 0 (the battery does
  not discharge; the sun is still stored). A discharge for the house sets *ESS max charge power limit* to 0 (the
  GX covers the house as always; an MPPT on the battery side still charges, the limit is the Multi's). A grid charge
  sets the max charge power to the rate and the *AC power setpoint*, which is the **grid's** power and not the
  battery's (positive takes from the grid), to the house's load now plus the rate. When the house then draws more
  the battery takes less, and it helps once the house alone passes the setpoint; the setpoint is not moved to
  follow the house, since it is a setting on the GX and Victron warns that writing it often wears the GX's flash.
  These are settings and stay if Home Assistant stops, so this charge runs under the usual cap, reckoned as if the
  Multis were all on one phase (the integration does not say how many there are).
* **Mode 3, external control, for a charge per phase** on a three-phase system. SpotNav sets *ESS mode (Hub4)* to
  external control and writes *AC power setpoint L1/L2/L3*. Such a setpoint is the power on that Multi's **AC
  input**, not a battery target ("Power to/from AC-input = Power to/from battery + Power to/from AC-output"), so
  SpotNav writes the load on that Multi's AC output plus the charge it wants there, and follows that load. Loads
  wired beside the Multis are seen by your grid meter and by the fuse rule. Handing back is one write, the ESS
  mode as it was found, and the GX runs all three phases again; SpotNav never leaves a setpoint behind, since
  outside external control the GX writes them itself.

**The watchdog, and what it does not change yet.** Victron: "These registers must be written once every 60
seconds, or the Multi will go into Passthru." SpotNav writes each setpoint again every 20 seconds. By that rule a
charge per phase would stop by itself within a minute of Home Assistant stopping, but nobody has seen it happen on
a plant, and a firmware where it does not act, or something else on the GX that writes the setpoints (a Node-RED
flow, a script), would leave them standing. So SpotNav counts on it nowhere: each phase's cap is reckoned as for
Sigenergy, with the house at its learned peak on that phase, and covers the whole setpoint that would stand (the
load behind the Multi plus the charge). On top of that each phase is filled only by what it has free **now**, to
4 A under the fuse, checked at every look, and a phase within 1 A of the fuse hands everything back at once.
If the Multis do fall back, they stay in passthru (the battery neither charges nor covers the house) until Home
Assistant is back and hands the ESS mode back, which it does first thing.

What a field test must show before the fallback may be counted on (per Venus OS version): a charge per phase of
at least 1 kW on each phase, then Home Assistant stopped mid-charge with the ESS mode still in external control;
within 70 seconds each Multi's AC input power falls to its AC output's load (the battery about 0 on each phase)
and stays there for 5 minutes, while the setpoints still read what SpotNav last wrote; the same after 10 minutes
of renewals, the charge never dropping meanwhile; and nothing else on the GX writing the setpoints (no Node-RED
flow, Dynamic ESS off). Until a report shows that, a charge per phase on a house whose learned peak fills a phase
gets nothing on that phase.

The check's per-phase step is the same as above, with the Multi's own power per phase read as its output power
less its input power.

**If Home Assistant never comes back, or something goes wrong, restore by hand** (in Home Assistant, or on the
GX under Settings → ESS):

* **ESS mode (Hub4)** back to the mode you had, *Optimized (with BatteryLife)* or *Optimized (without
  BatteryLife)*. Left in external control the Multis stay in passthru: no charging, no covering the house.
* **AC power setpoint** (the grid setpoint) back to your usual value. Left at the house plus a charge rate, the
  GX keeps importing that much and the battery stays full from the grid, day after day.
* **ESS max charge power** and **ESS max inverter power limit** back to what you had (-1 is no limit). A hold
  leaves the inverter limit at 0, a discharge for the house the charge power at 0.
* **Hub4 disable charge** and **Hub4 disable feed-in**: SpotNav turns both off for a charge per phase and does not
  turn them back, because outside external control the GX sets them itself. If either stays off while you had
  feed-in disabled on the GX, set it back.

**What the first field report should say,** beside the fallback test above: whether both Hub4 flags read what the
GX sets again within a minute of a hand-back (SpotNav counts on that, from Victron's documentation, not from a
plant); the setpoints the GX writes outside external control; and the Venus OS version.

**Sigen AI mode.** SpotNav can take the plant over from AI mode only after you have read this: *SpotNav takes over
the control; when you turn it off, the battery ends up in Maximum Self Consumption — turn AI mode on again in the
Sigenergy app if you want it back* ("SpotNav tar över styrningen; när du stänger av hamnar batteriet i Maximal
egenförbrukning — slå på AI-läget i Sigenergy-appen igen om du vill tillbaka"). Without that, a plant in AI mode is
only watched. In the battery section the checklist line **Read and accept** opens that text with **I understand**. When you turn control off after such a takeover, the card reminds you once to turn AI mode on again
("slå på AI-läget i Sigenergy-appen igen"); the reminder goes when the plant reads AI mode again.

**Cold battery** (Sigenergy). A battery in a carport or an unheated room can sit at a few degrees in winter, and a
cold battery takes much less charge (its cells must not be charged near 0 °C), so it misses the cheap hours. While
SpotNav runs the battery, its cells read below 5 °C for half an hour and the integration's **ESS Preheating Enable**
switch is off, the battery section says so, with a button to the battery's device to turn it on; the line goes once
the switch is on, the cells reach 7 °C or control stops. When the switch is disabled in Home Assistant (as it is by
default), the line asks you to enable it first so SpotNav can read it. A plant whose switch reads unavailable (the
protocol names only the Sigen PV M1-HYA/HYB for preheating) gets no line. SpotNav never switches the heater itself.

**Turning off** hands the battery back at once: control mode to Maximum Self Consumption, Remote EMS off, the
limits to what SpotNav found (a limit that was never set comes back as the battery's rating). The export limit is
not the battery switch's: it has [its own](solar-export.md). A setting you or an app changed meanwhile is left as
you set it. The battery plan keeps running in
shadow. SpotNav also hands back when Home Assistant stops or the site is reloaded, before anything else of the site
stops, and restores first after a start.

**If something goes wrong, restore by hand:** in Home Assistant set the Remote EMS control mode to Maximum Self
Consumption, turn the Remote EMS switch off and set the charge, discharge and export limits back to the values you
noted (and the three phase adjustments to 0, if you use charging per phase); then turn SpotNav's switch off.

**For the on-site tests only** (Developer tools, administrators): `spotnav.battery_control_test` runs one action
(`hold`, `grid_charge`, `discharge_house`, `export_discharge`, `curtail_export`, or `stop`) through the same write
path with every rule for some minutes and then hands back; it is refused while the site is shadow only unless
`force_test` is set. `spotnav.battery_control_settings` writes the site's battery control options, the owner's test
flags included (the card's section leaves those out). Neither is meant for everyday use.

The site's diagnostics show it all under `battery_control`: the options, the state, the check's report per firmware,
the actions allowed, what is missing, the notes, and the log of every write, refusal and would-do.

### EcoFlow PowerOcean (mapped, not yet tested on a plant)

For a PowerOcean through the HACS integration **EF-PowerOcean-TcpModbus** (`ef_powerocean_tcpmodbus`), which talks
Modbus TCP to the inverter on your network. Modbus must first be enabled on the inverter by EcoFlow's support or
your installer, and the inverter answers one Modbus client at a time: the other EcoFlow integration's local mode,
evcc or an installer's tool on the same inverter lock this one out. The other integration (`ecoflow_energy`) and
EcoFlow's cloud are read only in SpotNav; the cloud has no power setpoint for a PowerOcean at all.

SpotNav takes the battery only while the EcoFlow app runs it in self-consumption and the integration's **Modbus
Control** switch is off. It then uses the integration's own controls: it turns Modbus Control on, sets the power of
the mode it wants and then **Battery Mode**: *Hold Battery* for a hold (the battery stands idle; at 100 % this also
curtails the panels), *Charge Battery* at the rate for a grid charge, *Automatic* (the inverter's own
self-consumption) to cover the house, and *Discharge Battery* at the rate to sell. The power is the battery's own,
positive charging. A hand-back sets Battery Mode to Automatic first, which the inverter takes at once, then turns
Modbus Control off. **While Modbus Control is on the EcoFlow app is locked**, and it stays locked for about a minute
after: the inverter keeps obeying what it was last sent until its heartbeat runs out.

**The heartbeat.** The integration writes a heartbeat to the inverter every 20 seconds while Modbus Control is on,
and the inverter obeys commands only while it is fresh: about a minute after the last one it goes back to the app's
own settings by itself. So a command ends on its own when Home Assistant stops. It does **not** end while Home
Assistant runs and only SpotNav is stuck, since the integration keeps beating; SpotNav hands back actively when it
stops or unloads, and its fuse cap for a grid charge is reckoned as for Sigenergy (the house at its learned peak,
the whole charge on one phase, since EcoFlow does not say how a three-phase unit spreads it). The integration's
default powers are kept by the integration; SpotNav leaves the last rate it used in Charge Battery and Discharge
Battery, which matters only if you select those modes yourself.

**The hardware test first.** Nobody has run this on a plant yet, so on a PowerOcean SpotNav only shows what it would
do (as under shadow only, whatever the switch says) until **the site's own hardware test** (Settings → Support → Run
hardware test) has passed once on the battery and its result was approved with a control code
([Becoming a tester](#becoming-a-tester)); after a firmware or integration update SpotNav checks
the battery again by itself while control runs on, and asks for the hardware test again only if that check fails. The checklist says so.
Press Start:
the test waits by itself for a moment with the house using more than the sun gives, and besides the usual steps
(hold, a small grid charge, a discharge for the house, the hand-back) it **times the heartbeat**. It holds the
battery, turns Modbus Control off and nothing else, and watches how long the inverter keeps the hold before it
covers the house by itself (a minute or so is expected); then it turns Modbus Control on again (the integration
sends Automatic) and hands back. That step adds a few minutes, and the charge step shows the sign: the battery must
charge when told to. If the inverter keeps the hold past the window the step fails, and the battery is still taken
back.

**If something goes wrong, restore by hand:** in Home Assistant set Battery Mode to Automatic, then turn Modbus
Control off; after a minute the EcoFlow app works again. Then turn SpotNav's switch off.

### Solis (mapped, not yet tested on a plant)

For a Solis hybrid through the HACS integration **Solis Modbus** (`solis_modbus`, Pho3niX90), with the integration's
full sensor set (not "essential sensors only") and **one** inverter in Home Assistant (the integration's force
services are refused with more). SpotNav uses the inverter's own **remote-control force**, the same register an
outside energy manager uses, through the integration's services *Force battery charge*, *Force battery discharge*
and *Stop force charge/discharge*, which send the force, its power and its timeout in one go:

* **Hold:** a force charge at 0 W. The battery covers nothing; a sun surplus is still stored. A tester's log showed
  the battery flat at 0 W for over an hour this way while the house drew 1.8 kW.
* **Grid charge:** a force charge at the rate. Below its *Force Charge SOC* the inverter charges from the grid by
  itself at its *Force-charge Power Limitation*; SpotNav counts that against the fuse only while the battery is near
  that level. It needs **Allow Grid to Charge the Battery** on (SpotNav reads it and
  never sets it): with it off the plan's grid charge does not run. The battery's BMS has the last word: a cold pack may
  take only 10 A (about 3.7 kW at 370 V) whatever is asked, so SpotNav asks no more than the BMS's present charge
  limit times its voltage.
* **Cover the house:** the inverter's own Self-Use; nothing is written.
* **Sell:** a force discharge at the rate. The house is covered first and the rest goes to the grid.
* **Hand back:** the force stopped first, then *RC Force Battery Charge Power* and *RC Force Battery Discharge Power*
  set to **0** and *RC Timeout* back to the value it had. The powers never go back to the values found: whatever is
  left there runs in the next force anyone starts from the *RC Force Charge/Discharge* select, which sends them again.

Before each force SpotNav sets its power and the timeout and waits until the inverter's registers read them (the
number entities show a new value at once, before the inverter has it), so a force always runs at a power SpotNav saw
the inverter take, even if part of a write is lost on the way. SpotNav never writes the select.

**The dead-man.** The force ends by itself when the *RC Timeout* runs out. SpotNav sends it at 5 minutes (the
inverter's default) with every force and renews the force every 2 minutes, so if Home Assistant (or SpotNav) stops,
the inverter goes back to Self-Use within about five minutes. The renewals are bounded (800 a day; a whole day of
control is 720). This comes from the integration's source; the hardware test times it. A grid charge's fuse cap is
reckoned as for any battery that keeps its command (the house at its learned peak, the whole charge on one phase,
since Solis does not say how a three-phase unit spreads it): a five-minute timeout never lifts it. Nobody states
whether these registers are kept in flash; they are the ones an outside energy manager writes all day. If a renewal
is lost and the force lapses by itself, SpotNav hands back and starts again; three lapses in a day stop control for a
while.

**Another system that runs the force.** An energy manager (Enequi, for one) or SolisCloud's own strategy drives the
same register, and the inverter's time-of-use periods are its own schedule. SpotNav takes the battery only in the
*Storage Mode* "Self-Use" with no force running and every time-of-use period off. A force SpotNav did not write,
seen once, marks the battery as run by someone else: SpotNav then writes nothing more to the force, its powers or its
timeout (it does not even stop the other system's force), and its own plan keeps running in shadow as a benchmark.
That mark lasts until 48 hours after the last sign of the other system, unless you press **It was me – clear** in
the home battery section. One case looks the same and is treated so: if someone changes only *RC Force Battery
Charge Power* (or *Discharge Power*) by hand while SpotNav's own force runs, SpotNav writes nothing more and the
battery runs at that power until the *RC Timeout* ends the force (up to five minutes); SpotNav stops its own force
early only if a phase comes near the main fuse. After a restart SpotNav first waits for the integration to read the
force (at most five minutes) before it hands anything back, and leaves a force it did not write alone. The hardware test refuses to run while it stands, and a test waiting for its moment gives
up when another system takes the battery: pause the other system, clear the mark, then press Start.

**The hardware test first.** Nobody has run this on a plant yet, so on a Solis SpotNav only shows what it would do (as
under shadow only, whatever the switch says) until **the site's own hardware test** (Settings → Support → Run
hardware test) has passed on the inverter ([Becoming a tester](#becoming-a-tester)).
Press Start: the test waits by itself for a moment with the house using more than the sun gives (between 08 and 21
for the first pass, with a phone notice and a Stop button while it writes) and runs, in about twenty minutes:

1. **Hold:** the force charge at 0 W; the battery must stop covering the house.
2. **Small grid charge:** about 1 kW under the fuse cap and within the BMS's limit; the battery must charge.
3. **Discharge to the house:** back to Self-Use; the battery must cover the house.
4. **Stops by itself:** the hold again, renewed once after two minutes, then left alone while the test times how long
   the inverter keeps it before it covers the house by itself, judged by the force's own switches turning off (the
   house may change meanwhile; the battery's power is the cross-check). About five minutes after the renewal passes;
   sooner (the renewal did not restart the timer), much later or never fails, and the battery is stopped either way. On
   Solis this step must **pass**: if it could not be judged (the house's load moved, say), the test runs again later
   by itself and does not count as passed.
5. **Hand back:** the force stopped, both powers at 0, the timeout as found.

It aborts and hands back near the fuse, if the battery charges from the grid while it waits, or when anything is
written by someone else.

**If something goes wrong, restore by hand:** in Home Assistant set *RC Force Charge/Discharge* to *None*, *RC Force
Battery Charge Power* and *RC Force Battery Discharge Power* to 0 and *RC Timeout* to your value (5 minutes by
default). Then turn SpotNav's switch off. A force left standing also ends by itself once *RC Timeout* runs out.

### Kostal Plenticore (mapped, not yet tested on a plant)

For a Plenticore plus (G1, G2, G3), Plenticore BI or MP G3 with its battery through the HACS integration **Kostal
Plenticore Battery Control** (`kostal_modbus_control`, mbuhansen), which talks Modbus TCP to the inverter. SpotNav
reads the core Kostal Plenticore integration too, but never writes through it: it has no charge power and no
timeout. For control, the site's battery must be the Modbus integration's *Battery Power*.

**What must be set first.**

* The inverter's **Battery Management** set to *External via protocol (Modbus TCP)* in its web UI. That is an
  installer setting (the installer or service code). In any other mode the inverter ignores every write without an
  error, so SpotNav writes nothing until the integration's **Battery Management Mode** sensor reads *External battery
  management via Modbus protocol*. That sensor ships disabled; the card offers to enable it, with *Software Version*
  (the firmware the hardware test is kept per).
* The integration's **Modbus Timeout** the same as the timeout beside that setting in the web UI (30 or 60 seconds
  are common). SpotNav cannot read either.
* Nothing else writing to the inverter: it takes one controller (evcc, Tibber's bridge, Predbat's own Kostal
  automation and a Modbus automation of your own all use the same door).
* The card asks you to confirm that SpotNav's commands override the inverter's own battery control and time
  control: the Modbus integration does not show them.

SpotNav takes the battery only while none of the integration's switches (*Charge Start*, *Discharge Start*, *Block
Charge*, *Block Discharge*, *Inverter Control*) is on, and while Predbat, if installed, is not in a control mode (the
integration's *Charge Start* then runs Predbat's logic). Any of them on later is someone else's command: SpotNav hands
back and never turns another one's switch off. Battery Management on *digital I/O* (a ripple-control receiver or
another box) is someone else's too.

* **Hold:** *Block Discharge* on. The battery covers nothing and still stores a sun surplus. The integration holds
  it through the discharge limit by default; since the firmware of October 2026 the inverter's own standby draw (about
  200 W) may still come from the battery until the integration also holds the minimum SoC.
* **Grid charge:** *Set Charge Rate* at the rate (in steps of 100 W, at least 5 % below its maximum, which means
  "follow the battery's maximum" and moves with the battery's voltage), then *Charge Start*. A rate the integration
  took as "follow the maximum" anyway does not count as written, and *Charge Start* is then not turned on. The power
  is the battery's own (DC on a hybrid), so the sun counts towards it. *EMS Grid Protection*, if you turned it on,
  may lower it further.
* **Cover the house:** the inverter's own control; nothing is written.
* **No sale:** *Discharge Start* is never used.
* **Hand back:** the switch SpotNav turned on, off, then *Set Charge Rate* as SpotNav found it (a rate that followed
  the battery's maximum follows it again), so your own *Charge Start* runs as before.

The integration turns its other switches off when one goes on; SpotNav always turns its old switch off first, so it
never mistakes that for someone else. After *Charge Start* goes off the integration waits the timeout and 15 seconds
before its next write, so a hold right after a charge starts up to about 75 seconds late.

**The timeout.** The integration repeats each command to the inverter every half of its timeout, and the inverter goes
back to its own control by itself when no write comes for that long, so a command ends within about a minute of Home
Assistant stopping (the integration's switches also start off after a restart). It does not end while Home Assistant
runs and only SpotNav is stuck. SpotNav therefore does not count on it: a grid charge's fuse cap is reckoned as for a
battery that keeps its command (the house at its learned peak, the whole charge on one phase). The hardware test does
not time it, since no write stops the integration's repeats without also ending the command. Kostal does not state
whether these registers are kept in flash; the integration and evcc treat them as values that lapse with the timeout.

**The hardware test first.** Nobody has run this on a plant yet, so on a Plenticore SpotNav only shows what it would do
(as under shadow only, whatever the switch says) until **the site's own hardware test** (Settings → Support → Run
hardware test) has passed on the inverter ([Becoming a tester](#becoming-a-tester)).
Press Start: the test waits by itself for a moment with the house using more than the sun gives (between 08 and 21
for the first pass, with a phone notice and a Stop button while it writes) and runs, in about ten minutes:

1. **Hold:** *Block Discharge* on; the battery must stop covering the house.
2. **Small grid charge:** about 1 kW under the fuse cap; the battery must charge.
3. **Discharge to the house:** *Charge Start* off; the battery must cover the house again.
4. **Hand back:** both switches off as found.

It aborts and hands back near the fuse, if a write does not read back, if the battery does not do as told, or when
anything is changed by someone else.

**If something goes wrong, restore by hand:** turn *Charge Start* and *Block Discharge* off; the inverter returns to
its own control within its timeout. Then turn SpotNav's switch off.

### Fronius Gen24 (mapped, not yet tested on a plant)

For a Fronius Gen24, Gen24 Plus, Tauro or Verto Plus with a BYD or Fronius Reserva battery, through Home Assistant's
own **Fronius** integration (`fronius`) in **Home Assistant 2026.11 or later**, which talks Modbus TCP to the
inverter. In the inverter's web UI, under Communication → Modbus, Modbus TCP and **Inverter control via Modbus** must be
on: without them the integration shows no battery limits and SpotNav only reads the battery. The HACS integration
`fronius_modbus` is not used.

SpotNav uses the integration's two battery limits, each a share of the battery's rated power (the *Battery power limit
reference*) and each acting only while its switch is on. A negative limit forces the other direction:

* **Hold:** *Battery discharge power limit* at 0 % and *Battery discharge power limiting* on. The battery covers
  nothing; a sun surplus is still stored.
* **Grid charge:** *Battery discharge power limit* below 0, at the rate's share of the rated power (rounded down, in
  steps of 1 %), with its switch on: a forced charge at least that fast. *Battery charge power limit* at the same
  share, with its switch on, caps it: neither the sun nor the grid takes the charge above the rate. It needs **Battery grid charging** on in Home Assistant and *Battery charging from other sources (including public
  grid)* on in the web UI. SpotNav reads the first and never sets it; the second cannot be read, and a charge that
  does not come shows only as a battery that does not charge, which SpotNav then hands back.
* **Cover the house:** *Battery charge power limit* at 0 % and *Battery charge power limiting* on: the battery covers
  the house and stores nothing.
* **Sell:** not yet. It would be a negative charge limit, which nobody has seen on a plant.
* **Hand back:** both switches off first (the inverter's own settings take over again), then both limits back to the
  values found (a negative one found is put back as 100 %, no limit).

The limits are written before their switches, and when both are on (a grid charge, +p and -p, which add up to 0) a
change of rate writes the limit that rises first: the integration refuses two limits that are both on and add up to
less than 0. The rate is a share of the rating the inverter reports;
while that reads nothing, no grid charge runs. SpotNav never writes the *Battery minimum reserve* or grid charging, and
it starts only while both limit switches are off: one that is on is someone else's (evcc, a script, an automation).
Time-of-use schedules in the web UI and the Energy Cost Assistant in Solar.web cannot be seen from Home Assistant, so
the checklist asks you to accept that SpotNav's limits override them; turn them off.

**When Home Assistant stops.** The inverter keeps a limit until it is changed. The integration's option **Revert the
power and battery limits if Home Assistant stops** (Settings → Devices & services → Fronius → Reconfigure; off by
default) has the inverter drop the limits about an hour after Home Assistant last sent them; turn it on. It is the
integration's own timer, renewed by the integration every 15 minutes while Home Assistant runs, not by SpotNav, so it
does not end a limit while Home Assistant runs and only SpotNav is stuck. SpotNav hands back when it stops or unloads,
and a grid charge's fuse cap is reckoned as for a battery that keeps its command (the house at its learned peak, the
whole charge on one phase, since Fronius does not say how a three-phase Gen24 spreads a forced charge). Nobody states
whether these registers are kept in flash; SpotNav writes them only when something changes.

**The hardware test first.** Nobody has run this on a plant yet, so on a Fronius SpotNav only shows what it would do
(as under shadow only, whatever the switch says) until **the site's own hardware test** (Settings → Support → Run
hardware test) has passed on the battery and its result was approved with a control code
([Becoming a tester](#becoming-a-tester)). Press Start: the test waits by itself for a
moment with the house using more than the sun gives (between 08 and 21 for the first pass, with a phone notice and a
Stop button while it writes) and runs, in about ten minutes:

1. **Hold:** the discharge limit at 0 %; the battery must stop covering the house.
2. **Small grid charge:** the discharge limit below 0 and the charge limit at the same share, about 1 kW under the
   fuse cap; the battery must charge.
3. **Discharge to the house:** the charge limit at 0 %, the discharge limit's switch off; the battery must cover the
   house.
4. **Hand back:** both switches off, both limits as found.

There is no step that times the revert option: an hour is longer than any test. The firmware the result is kept
under is the battery storage's software version as the Fronius device page shows it (a Gen24 itself shows none), so an
update of the inverter's own firmware is not noticed: after one, press Start again. It aborts and hands back near the
fuse, if a write does not read back or when anything is written by someone else.

**If something goes wrong, restore by hand:** in Home Assistant turn *Battery charge power limiting* and *Battery
discharge power limiting* off, set both limits to your values (100 % if you never changed them). Then turn SpotNav's
switch off.

### Becoming a tester

Control of every brand but Sigenergy (SolarEdge, Huawei, SolaX, Solinteg, Victron, GoodWe, Tesla Powerwall, Ferroamp,
FoxESS, EcoFlow, Marstek, Solis, Kostal Plenticore, Fronius and Deye / Sunsynk) has not yet been tested on a real
plant. It is tested together with users, one plant at a time, so an untried recipe is never switched on by mistake
and every early plant is known and looked at. Sigenergy needs none of this.

**Control that already runs stays on.** A plant of SolarEdge, Huawei, SolaX, Solinteg, Victron, GoodWe, Tesla
Powerwall or Ferroamp where SpotNav's check had ever passed for the brand when this release came counts as tested and
as having its code: nothing changes for it, and the card shows no Tester row. It does not matter whether control was
switched on at the update, or whether the firmware or the integration was updated at the same time: the check then
runs on the new version by itself, and control runs on meanwhile. Every other plant of these brands goes through the steps below. A
plant's own hardware test stops its control only when a step fails, never when the test could not judge.

1. **Become a tester.** In the card's Settings, the home battery section shows a **Tester** row for these brands
   (administrators only): *SpotNav can read this battery. Control is being tested together with users.* Tap it, give
   an e-mail address (required here), tick the consent line and send. That sends a report with the debug file and
   the battery's brand, model, firmware, integration and version, recipe revision, phases and main fuse, and turns
   [beta testing](beta-testing.md) on with that address. Until the sign-up the hardware test is not offered for the
   battery (Support → Hardware test says why).
2. **Run the hardware test.** Settings → Support → Hardware test, then Start, once. Nothing else is asked of you: the
   test waits for a suitable moment by itself, runs only in waking hours (08–21), tells your phones while it runs
   (with a Stop button) and always hands the battery back. Its result is sent by itself when it ends, with the
   battery's log and history over the test, while Beta tester is on. The card then says *The result has been sent. You get an answer by
   e-mail.*
3. **Enter the control code.** SpotNav's developer reads the test's log and answers by e-mail: with a **control code**
   when the test passed and looks right, otherwise with what was seen (and possibly a fix in a later release). Paste
   the code under Support → Hardware test (spaces and line breaks do not matter); it is taken only once the hardware
   test has passed on this battery. Control needs both: the passed hardware test on this plant and the code. Then switch control on as for any brand.

The code works only on this Home Assistant installation and this battery brand; it is checked in Home Assistant
itself (it is signed), nothing is asked of a server, and again at each start of Home Assistant (a code that no
longer checks out is dropped, and control waits for a code again). One code per plant and brand is enough: after a firmware,
integration or recipe update SpotNav checks the battery by itself while control runs on, as for every brand, and only if that check fails
does the checklist ask for the hardware test again (no new code). SpotNav is open source, so the code is no copy
protection: it is a safety belt.

**Stopping.** Untick Beta tester under Support at any time: from then on nothing more is sent, not even a hardware
test's result, and the sign-up ends (a first test then waits for a new sign-up). A beta period that runs out by
itself ends the sign-up the same way. A code already entered keeps working (it is your battery), and control can be
switched off as always. While beta testing is on, its nightly reports
run until 30 days after the code was entered, then stop by themselves.

**A problem found later.** There is no remote switch-off. If a problem shows up after codes went out, the next
release holds that brand, firmware or recipe revision back (the checklist then says *recipe blocked*), which also
voids the plant's stored pass: once the block is lifted the hardware test must pass again (the code already entered
stays). One code serves every SpotNav site of the same brand on one Home Assistant; each still needs its own passed
test.

## The signal

Two binary sensors, both named "Charging from the grid":

| Entity | Where | On when |
| --- | --- | --- |
| `binary_sensor.<charger>_charging_from_the_grid` | each charger | the charger is charging and the energy is meant to come from the grid |
| `binary_sensor.<site>_charging_from_the_grid` | each site | any charger of the site has its own sensor on |

Use the site sensor in most cases. The exact entity ids follow your charger and site names.

A charger's sensor is **on** during a planned (cheapest) window, the grid part of a hybrid plan and a
manual Start or Charge now. It is **off** when the charger is idle or paused, and while it charges
from solar surplus (the solar strategy, or the solar part of hybrid). A manual start is always on,
even under the solar strategy, because you asked for charging now.

If SpotNav cannot tell where a running charge comes from (a charger that started by itself), the
sensor is on only while the site meter reads grid import.

Attributes of a charger's sensor:

* `source`: `plan_window`, `hybrid_grid`, `manual`, or `grid_import` (not attributable, site importing).
* `window_end`: when the active plan window ends (ISO time), if there is one.
* `power_w`: the charger's power in watts, if the charger has a power sensor.

The site sensor has `charger_ids`, the entries of the chargers that are on now.

## Predbat

Predbat has a `car_charging_now` setting that accepts an on/off sensor. While it reads on, Predbat
holds the battery itself so that it does not discharge into the car (see Predbat's
[car charging documentation](https://github.com/springfall2008/batpred/blob/main/docs/car-charging.md)).
In `apps.yaml`:

```yaml
car_charging_now:
  - binary_sensor.<site>_charging_from_the_grid
```

Replace the entity id with your site sensor. If you want the battery to be allowed to supply the car,
turn on `switch.predbat_car_charging_from_battery` in Predbat.

## EMHASS

EMHASS has no input for this, so use an automation: while the sensor is on, hold the battery (see
below), and restore it when the sensor turns off. You can also subtract the planned car load from
the house load you give EMHASS.

## Your own automation

Replace the entities with what your battery integration offers.

```yaml
automation:
  - alias: Hold the battery while the car charges from the grid
    triggers:
      - trigger: state
        entity_id: binary_sensor.<site>_charging_from_the_grid
        to: "on"
    actions:
      - action: number.set_value
        target:
          entity_id: number.<battery>_max_discharge_power
        data:
          value: 0

  - alias: Release the battery
    triggers:
      - trigger: state
        entity_id: binary_sensor.<site>_charging_from_the_grid
        from: "on"
    actions:
      - action: number.set_value
        target:
          entity_id: number.<battery>_max_discharge_power
        data:
          value: 5000 # your battery's normal limit
```

Always restore the battery on every path. Trigger the release on `from: "on"` (as above, so it also
runs when the sensor becomes unavailable), and also on Home Assistant start, because a restart while
the battery is held would otherwise leave it held. Check that your battery integration really obeys
the setting you write: some firmware ignores it in some modes.

## Forecasts for other energy tools

SpotNav shows its forecasts in the shape energy optimisers such as HAEO and EMHASS read, and offers the
corrected solar forecast to the Energy dashboard. Nothing here is a setting: it is a view of figures SpotNav already
makes. The house forecast and the corrected sun do drive SpotNav's own plans; publishing them here changes nothing in
those.

| Where | State | Attribute `forecast` (`[{time, value}]`, never recorded) |
|---|---|---|
| **Price now** (the electricity price device) | The running interval's price, currency per kWh | The price of each interval from now on, in the sensor's unit |
| **House load forecast** (the site, diagnostic) | The running hour's expected load, kW | One row per hour for the next day, kW. `basis` says whether it is the whole load (`gross`) or the import less the chargers (`net`) |
| **Solar forecast** (the site, diagnostic; only where a solar forecast is chosen) | The running hour's expected sun, kW | One row per hour, kW (an hour's kWh is its mean kW). `basis` is `corrected` once the correction is learnt, else `raw` |

`time` is an ISO 8601 time with its time zone, `value` is in the sensor's unit. The house load also stays
on the site state sensor as `forecast: [{start, kw}]`.

The Energy dashboard can take SpotNav's corrected solar forecast as a solar source's forecast: choose the
site's entry as the forecast integration. It answers only once the correction is learnt (before that the
dashboard shows nothing from SpotNav, not an unchecked figure), as Wh per hour from the running hour, and
SpotNav never uses it as a source itself. The picker lists every SpotNav entry; only a site that has a solar
forecast chosen gives anything.

## Mirrored from another Home Assistant

Some inverters take only one Modbus client, so the battery's integration (SolaX Modbus, say) runs on another Home
Assistant and reaches this one through [Remote Home-Assistant](https://github.com/custom-components/remote_homeassistant)
(HACS). SpotNav reads such a battery, read only:

* **How the brand is found.** Remote Home-Assistant keeps the other instance's entity ids (after the entry's
  entity prefix, which SpotNav takes off) but not its unique ids or devices. SpotNav tries each brand it knows on the
  mirrored entities by the brand's own keys in those ids, with the unit each needs (the level in %, the size in Wh or
  kWh). A brand counts only when the level is found and the battery entity reads a power; the brand that finds the
  most wins. When two brands find as much, the battery stays unknown, and the diagnostics say which tied
  (`detected` → battery → `mirror`).
* **What you see.** The level, the size and the counters the brand's integration names, read on the other instance.
  A size or a rating that is not mirrored (SolaX Modbus disables its size by default) is learnt from the plan's own
  quarter hours, as for any battery. The firmware is not mirrored.
* **Never controlled.** The battery's settings card says *Read through Remote Home-Assistant: read only*. SpotNav plans
  with the battery as a comparison and writes nothing to it, whatever the brand: a write would go to the other instance
  outside its own integration's checks.
* **Keep the entity ids.** A renamed entity on the other instance is not found. Mirror the battery's power, level and
  size at least; the mode selects let SpotNav see who runs the battery.
* **The power's sign** is your site setting. Where the battery entity is the brand's own battery power entity, the
  diagnostics say whether the setting agrees with the brand's convention (`power_sign`).

## When another system runs your battery

Many home batteries are run by something else: the maker's own optimiser (Sigen AI mode, FusionSolar
time-of-use, SEMS), a service that sells the battery's flexibility (CheckWatt, Greenely, Tibber Grid
Rewards), a planner (Predbat, EMHASS) or an automation of yours. One controller per battery: SpotNav
notices the others and only watches. Unless you turn SpotNav's battery control on (off by default), SpotNav writes to
no battery at all, so every battery is only watched; with it on, the detection keeps SpotNav off a battery another
system runs.

* **A planner or CheckWatt set up in Home Assistant**: the battery is watch-only while it is there.
* **A command SpotNav did not write, seen standing** where a recipe holds the whole command in one place (Solis's RC
  force): marked watch-only at once, since SpotNav never leaves one of its own standing. It ends as the next point
  does (48 hours after the last sign, or **It was me – clear**).
* **The battery's settings change three times within 24 hours without SpotNav writing them, or in plain
  self-consumption it charges from the grid or discharges into export by more than 1 kW for ten minutes in a
  row** (and its level moves the same way): something outside SpotNav drives it, and SpotNav marks it watch-only. This ends by itself 48 hours after the
  battery last showed it. If it was you (you changed the battery's modes yourself, say during a test), an
  administrator can end it at once with **Det var jag – rensa** (It was me – clear) beside the note in the battery's
  settings; the changes counted so far are forgotten with it. Tibber or Greenely set up in Home Assistant is named as the likely source but
  never marks the battery by itself.

The behaviour rule is deliberately narrow: the site must also import (or export) more than 1 kW, the level must move at
least 1 point the same way, a charge within 5 points of the battery's own lowest level is ignored as its maintenance
charge, and under the battery's own optimiser only changes of mode count.

SpotNav does not raise a notice for this in Settings → Repairs: there is nothing you need to do. It
writes one line to the log (at INFO) when it starts watching only and one when it stops, and keeps it
in the diagnostics and in what SpotNav detected.

A watch-only battery still gets SpotNav's battery plan as a comparison, and SpotNav never writes to it.
The site's diagnostics show what was seen (`battery.outside_control`) and, under `detected`, what
SpotNav found for the site, the battery included, with what usually runs that brand.

For chargers, a short pause from such a service (Tibber Grid Rewards pauses a charge up to five times, about
ten minutes each) is expected: SpotNav keeps its plan and does not tell you (see
[notifications](notifications.md)).

## No export at a negative price

When an exported kWh would cost you money, SpotNav can set the inverter's export limit to zero and put it back
afterwards. It has its own switch (**Settings → Solar → No export at negative prices**) and its own page:
[No export at negative prices](solar-export.md). It works with or without a battery. The battery control switch
does not turn it on, and it does not wait for the battery's check, hardware test or control code.
