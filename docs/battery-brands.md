---
title: Home battery brands in detail
description: Brand by brand, what SpotNav's battery control writes, what must be set first, what it never touches and how to restore by hand.
order: 32
section: House and battery
---

# Home battery brands in detail

The technical side of [Home batteries](home-battery.md): for each brand, what SpotNav's battery control writes,
what must be enabled or set first, what it leaves alone, and how to put things back by hand. Read the main page first:
the switch, the check, the safety rules and becoming a tester are the same for every brand and are described there.
The table of brands is at [Brands at a glance](home-battery.md#brands-at-a-glance).

In short:

- Only Sigenergy has run on a real plant. Every other brand is mapped from its integration's source and waits for a
  first tester: nothing is written until a hardware test has passed on your plant and its result was approved with a
  control code ([Becoming a tester](home-battery.md#becoming-a-tester)).
- Before you turn control on, note the values the page names under **Restore by hand** for your brand.
- How a grid charge is kept under the main fuse is on its own page: [Grid charging and the main fuse](battery-grid-charging.md).

## Control entities per brand

The entities SpotNav's control writes, per brand. Where they ship disabled, turning **Battery control** on offers
to enable them (see [Turning on the control entities](home-battery.md#turning-on-the-control-entities)).

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

## Sigenergy

### Before control can run

**Sigenergy** (the Sigenergy Local Modbus integration) is the first brand. Before control can run:

* The integration must allow writes (its own option; it is read-only by default and then drops every write
  without an error, which SpotNav notices: every write is read back).
* The plant's Remote EMS switch, Remote EMS control mode, charge and discharge limits and plant maximum power must be
  enabled in Home Assistant (they are disabled by default); for the export part also Grid Export Limitation. You do not
  have to do it by hand: see [Turning on the control entities](home-battery.md#turning-on-the-control-entities).
* No other controller may run the battery (Predbat, EMHASS, CheckWatt, a VPP in Remote EMS): SpotNav then only
  watches (see [When another system runs your battery](home-battery.md#when-another-system-runs-your-battery)).

### How SpotNav holds the plant

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

### Sigen AI mode

**Sigen AI mode.** SpotNav can take the plant over from AI mode only after you have read this: *SpotNav takes over
the control; when you turn it off, the battery ends up in Maximum Self Consumption — turn AI mode on again in the
Sigenergy app if you want it back* ("SpotNav tar över styrningen; när du stänger av hamnar batteriet i Maximal
egenförbrukning — slå på AI-läget i Sigenergy-appen igen om du vill tillbaka"). Without that, a plant in AI mode is
only watched. In the battery section the checklist line **Read and accept** opens that text with **I understand**. When you turn control off after such a takeover, the card reminds you once to turn AI mode on again
("slå på AI-läget i Sigenergy-appen igen"); the reminder goes when the plant reads AI mode again.

### Cold battery

**Cold battery** (Sigenergy). A battery in a carport or an unheated room can sit at a few degrees in winter, and a
cold battery takes much less charge (its cells must not be charged near 0 °C), so it misses the cheap hours. While
SpotNav runs the battery, its cells read below 5 °C for half an hour and the integration's **ESS Preheating Enable**
switch is off, the battery section says so, with a button to the battery's device to turn it on; the line goes once
the switch is on, the cells reach 7 °C or control stops. When the switch is disabled in Home Assistant (as it is by
default), the line asks you to enable it first so SpotNav can read it. A plant whose switch reads unavailable (the
protocol names only the Sigen PV M1-HYA/HYB for preheating) gets no line. SpotNav never switches the heater itself.

### Restore by hand

**If something goes wrong, restore by hand:** in Home Assistant set the Remote EMS control mode to Maximum Self
Consumption, turn the Remote EMS switch off and set the charge, discharge and export limits back to the values you
noted (and the three phase adjustments to 0, if you use charging per phase); then turn SpotNav's switch off.

## GoodWe and Tesla Powerwall

**GoodWe and Tesla Powerwall: mapped, never run on a device.** Both are switched on by the same switch and checked by
the same routine as the others, and both are untested: nothing is written until a hardware test has passed on the
plant and its result was approved with a control code ([Becoming a tester](home-battery.md#becoming-a-tester)).

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

## SolarEdge, Huawei and SolaX

**SolarEdge, Huawei and SolaX: mapped, not yet tested on a plant.** The same holds for these three: the hardware test
and a control code come first ([Becoming a tester](home-battery.md#becoming-a-tester)). SolarEdge runs only in Remote Control with
its storage command timeout (30 minutes, renewed); Huawei through the forcible charge services and the battery's
power limits (installer password); SolaX through the integration's remote control, renewed by its autorepeat.

## Ferroamp

**Ferroamp (EnergyHub), untested.** Nothing is written until a hardware test has passed on the hub and its result was
approved with a control code ([Becoming a tester](home-battery.md#becoming-a-tester)). Control goes through the `ferroamp` integration's services (Ferroamp's MQTT API,
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

## Sofar and Growatt (read only)

**Read only: Sofar and Growatt.** SpotNav reads these two through their plugins of the SolaX Modbus
integration (level and power, and what each states of its size and floor: see
[The battery plan](battery-plan.md#what-the-plan-needs)), so the site can tell the car's draw from the battery's and the
battery plan can run in shadow. It has no control for them and writes nothing to them. Both are read from the
integration's source and not yet tested on a battery: [tell us](supported.md) how yours reads.

## FoxESS

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
  [Becoming a tester](home-battery.md#becoming-a-tester)); until then control only logs what it would do,
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

## Deye, Sunsynk and Sol-Ark

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
  [Becoming a tester](home-battery.md#becoming-a-tester)); until then control only logs what it would
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

## Solis (mapped, not yet tested on a plant)

**Solis (hybrid inverters such as the S6-EH3P): control mapped, never run on a plant.** SpotNav reads these through
the Solis Modbus integration (`solis_modbus`): the battery's power (*Battery Power Net*) and level, its floor
(*Overdischarge SOC*, the on-grid one) and the grid meter. The ratings are the BMS's charge and discharge current limits
times the BMS's voltage, an estimate; the size is learnt (the integration's *Battery Rated Capacity* is a setting in Ah,
not the bank the BMS reports). SpotNav counts the battery as running itself only in the *Storage Mode* "Self-Use" with
neither *RC Force Battery Charge* nor *RC Force Battery Discharge* on; with the integration's "essential sensors only"
option those entities are missing and SpotNav cannot tell (and has nothing to control). A time-of-use period switched
on beside "Self-Use" is the inverter's own schedule: SpotNav then only watches. The control follows.

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
hardware test) has passed on the inverter ([Becoming a tester](home-battery.md#becoming-a-tester)).
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

## Marstek

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
result was approved with a control code ([Becoming a tester](home-battery.md#becoming-a-tester)): until
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
  the battery is plugged into (see [the grid charge cap](battery-grid-charging.md#the-grid-charge-cap-and-the-houses-peaks)). *Charge to SoC* is never written.
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

## Solinteg

**Solinteg (MHT, M2HT; also M-TEC Energy Butler and Wattsonic): mapped, never run on a device.** SpotNav has the
commands for the Solinteg plugin of the SolaX Modbus integration behind the same switch and the same check, read from
the plugin's source and from owners' notes. Nobody has run them on an inverter yet; the first to do so is a pilot.
Nothing is written until a hardware test has passed on the inverter and its result was approved with a control code
([Becoming a tester](home-battery.md#becoming-a-tester)).
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
  [The grid charge cap and the house's peaks](battery-grid-charging.md#the-grid-charge-cap-and-the-houses-peaks), and because nothing says how the inverter spreads such a charge
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

## Victron ESS (mapped, not yet tested on a plant)

For a GX device with a MultiPlus or Quattro and the ESS assistant, through the **Victron GX** integration in Home
Assistant core or the HACS **Victron MQTT**. Nobody has run it on a plant yet: it is built from Victron's own page
"ESS mode 2 and 3" and the integrations' source, it sits behind the same switch and the same check, and a
[field report](supported.md) is what it needs. Nothing is written until a hardware test has passed on the plant and
its result was approved with a control code ([Becoming a tester](home-battery.md#becoming-a-tester)). The community Modbus integration (`victron`) is read only: its
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

The check's per-phase step is the same as for Sigenergy ([Grid charging per
phase](battery-grid-charging.md#grid-charging-per-phase)), with the Multi's own power per phase read as its output power
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

## EcoFlow PowerOcean (mapped, not yet tested on a plant)

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
([Becoming a tester](home-battery.md#becoming-a-tester)); after a firmware or integration update SpotNav checks
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

## Kostal Plenticore (mapped, not yet tested on a plant)

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
hardware test) has passed on the inverter ([Becoming a tester](home-battery.md#becoming-a-tester)).
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

## Fronius Gen24 (mapped, not yet tested on a plant)

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
([Becoming a tester](home-battery.md#becoming-a-tester)). Press Start: the test waits by itself for a
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
