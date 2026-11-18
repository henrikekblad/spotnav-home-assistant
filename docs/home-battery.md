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
stop export when selling costs money. **It is off by default, and in this release it is still being tested on site:
even switched on, it only logs what it would do until the tests are done (the line under the switch says "Test:
SpotNav controls nothing yet and only logs what it would do").** The switch is in the
settings popover's **Home battery** section, shown when SpotNav finds a battery: from the house card's gear, the heat
card's gear or the charging card's **Settings** (administrators only). The section also has:

* one line under the switch saying what control does now (off, test mode, checking, waiting for a suitable moment and
  why, on, stopped, or only watching while the AI mode is not yet accepted);
* the checklist of what you still have to do (enable an entity, turn on the firmware sensor, accept the AI-mode
  takeover), each in plain words;
* the check's progress while it runs, and its result per step (hold, charge from the grid, cover the house, hand back:
  passed, failed or not judged yet) with what SpotNav may then do;
* the reminder to turn AI mode on again after a takeover ended, and **Allow selling from the battery** (off by
  default).

**Sigenergy** (the Sigenergy Local Modbus integration) is the first brand. Before control can run:

* The integration must allow writes (its own option; it is read-only by default and then drops every write
  without an error, which SpotNav notices: every write is read back).
* The plant's Remote EMS switch, Remote EMS control mode, charge and discharge limits and plant maximum power must be
  enabled in Home Assistant (they are disabled by default); for the export part also Grid Export Limitation.
* No other controller may run the battery (Predbat, EMHASS, CheckWatt, a VPP in Remote EMS): SpotNav then only
  watches (see below).

**Read only: Sofar, Growatt and Solinteg.** SpotNav reads these three through their plugins of the SolaX Modbus
integration (level and power, and what each states of its size and floor: see
[The battery plan](battery-plan.md#what-the-plan-needs)), so the site can tell the car's draw from the battery's and the
battery plan can run in shadow. It has no control for them and writes nothing to them. All three are read from the
integration's source and not yet tested on a battery: [tell us](supported.md) how yours reads.

**Switching on starts a check, not control.** SpotNav first checks that the battery obeys, each step through the
same write path control uses: hold (the battery stops discharging), charge 2 kW from the grid, cover the house, and
hand back (every setting back as found). Each step waits up to 5 minutes for the writes, 5 minutes to settle and
takes three readings a minute apart: about 40 minutes in all. It runs at a suitable moment: the battery between 30
and 80 % and discharging (so a hold can be seen), no car charging, room under the fuse; a weekday evening is
typical. A step the battery cannot show at the moment (nothing to stop, nearly full) is tried again at a later
suitable moment, not counted as a failure; after five runs without an answer the check gives up.

**Two ways to hold a Sigenergy plant.** Which of the plant's limits act in which Remote EMS mode depends on the
firmware: Sigenergy's Modbus Protocol V2.7 says the charge limit acts only in Command Charging (modes 3 and 4) and the
discharge limit only in Command Discharging (5 and 6); V2.9 (May 2026) says both act in every mode. So the check
tries the hold two ways, one at a time, and keeps the first that works:

1. **Maximum Self Consumption with the discharge limit at 0** (current firmware). Under a peak SpotNav lifts the
   limit and the plant covers each phase itself.
2. **Command Charging (PV First) with the charge limit at 0** (older firmware, where the discharge limit does
   nothing in Maximum Self Consumption). The protocol's Appendix 6 names this mode "Command charging (consume PV
   power first)": a charge at the charge limit, from the sun first and then from the grid. With the limit at 0 it
   charges nothing, and no Command Charging mode discharges, so the battery stands idle and a sun surplus is
   exported while the hold lasts. That is also what the plant keeps doing if Home Assistant stops in the middle of a
   hold. Under a peak SpotNav switches the mode back to Maximum Self Consumption; a mode change can take the plant a
   little longer than a limit (the plant's *active power regulation gradient*, register 40049, sets how fast it
   ramps; SpotNav only reads about it, never writes it). If the headroom is not back within 30 seconds the battery is
   handed back.

The hold is judged on what the battery does while the house draws: it must not discharge, and with the first way it
should still store sun. With the second way the battery must stand idle: if it charges more than 0.5 kW for a
minute, the plant did not take the limit and the hold is handed back at once, in the check and in control alike
(judged on the battery's own power, so it works without a grid reading). The check then fails; in control the block
clears by itself after six hours. The check moves on to the second way only when the first was simply ignored, and
only once the battery discharges again. The check's result says which way passed; control uses that one. A firmware
update runs the check again, both ways if needed. A discharge to the house stays Maximum Self Consumption with the
charge limit at 0 (V2.7 has no mode for "cover the house, store nothing"; where the limit is ignored the battery
also stores a sun surplus, as it does normally). Other protocol changes worth knowing: V2.8 removed register 41500
(Remote EMS dispatch enable, not used by SpotNav) and V2.9 added V2G (0x08) to the Remote EMS control modes.

**The check decides what SpotNav may do.** Hold and the hand-back must pass for any control; grid charging only if the
charge step passed, covering the house only if that step passed. A failed step leaves control off, with one line
saying what did not work (for example "the battery did not stop discharging when held"); turning the switch off and
on again runs the check again. The result is kept per firmware and integration version: after a firmware update
(or a new version of the integration) SpotNav hands the battery back and checks again at the next suitable evening.

**What control does.** It follows the battery plan's running quarter hour, and holds the battery when the car
charges from the grid in a quarter hour the plan leaves to the battery. Every write is read back; every command's
effect is judged; anything unexpected hands the battery back (in Maximum Self Consumption) and stops. Safety rules:

* **The fuse.** A grid charge is clamped to the live headroom under the main fuse. Because Sigenergy has no
  dead-man, it is also capped so that the charge and the house's peak stay under the fuse on every phase even if
  Home Assistant stops while it runs (below). Under a hold the battery is let go at once when a phase nears the fuse
  (a kettle, the oven), and a hold meant to leave the battery idle that charges it anyway is handed back within a
  minute.
* **What backs off first** when the fuse is short: the battery's grid charge, then the car; the car first when its
  departure has room. With the solar priority "battery first" the car backs off before the battery.
* **Selling from the battery** (discharging to the grid) is off unless you allow it in the battery section.
* **At most 96 writes a day**, outside hand-backs.

### The grid charge cap and the house's peaks

The cap answers one question: how much may the battery draw from the grid so that no phase passes the main fuse if
Home Assistant stops and the command stays? It is the fuse, less a margin, less what the chargers may draw, less the
house's peak; what is left is spread over the plant's phases. The house's peak is what SpotNav has to learn.

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

Once the plant's own grid import limit is shown to act (the on-site test), that limit guards the fuse and this cap is
not used.

**Sigen AI mode.** SpotNav can take the plant over from AI mode only after you have read this: *SpotNav takes over
the control; when you turn it off, the battery ends up in Maximum Self Consumption — turn AI mode on again in the
Sigenergy app if you want it back* ("SpotNav tar över styrningen; när du stänger av hamnar batteriet i Maximal
egenförbrukning — slå på AI-läget i Sigenergy-appen igen om du vill tillbaka"). Without that, a plant in AI mode is
only watched. In the battery section the checklist line **Read and accept** opens that text with **I understand**. When you turn control off after such a takeover, the card reminds you once to turn AI mode on again
("slå på AI-läget i Sigenergy-appen igen"); the reminder goes when the plant reads AI mode again.

**Turning off** hands the battery back at once: control mode to Maximum Self Consumption, Remote EMS off, the
limits to what SpotNav found (a limit that was never set comes back as the battery's rating), the export limit to
what it found. A setting you or an app changed meanwhile is left as you set it. The battery plan keeps running in
shadow. SpotNav also hands back when Home Assistant stops or the site is reloaded, before anything else of the site
stops, and restores first after a start.

**If something goes wrong, restore by hand:** in Home Assistant set the Remote EMS control mode to Maximum Self
Consumption, turn the Remote EMS switch off and set the charge, discharge and export limits back to the values you
noted; then turn SpotNav's switch off.

**For the on-site tests only** (Developer tools, administrators): `spotnav.battery_control_test` runs one action
(`hold`, `grid_charge`, `discharge_house`, `export_discharge`, `curtail_export`, or `stop`) through the same write
path with every rule for some minutes and then hands back; it is refused while the site is shadow only unless
`force_test` is set. `spotnav.battery_control_settings` writes the site's battery control options, the owner's test
flags included (the card's section leaves those out). Neither is meant for everyday use.

The site's diagnostics show it all under `battery_control`: the options, the state, the check's report per firmware,
the actions allowed, what is missing, the notes, and the log of every write, refusal and would-do.

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

## When another system runs your battery

Many home batteries are run by something else: the maker's own optimiser (Sigen AI mode, FusionSolar
time-of-use, SEMS), a service that sells the battery's flexibility (CheckWatt, Greenely, Tibber Grid
Rewards), a planner (Predbat, EMHASS) or an automation of yours. One controller per battery: SpotNav
notices the others and only watches. Unless you turn SpotNav's battery control on (off by default), SpotNav writes to
no battery at all, so every battery is only watched; with it on, the detection keeps SpotNav off a battery another
system runs.

* **A planner or CheckWatt set up in Home Assistant**: the battery is watch-only while it is there.
* **The battery's settings change three times within 24 hours without SpotNav writing them, or in plain
  self-consumption it charges from the grid or discharges into export by more than 1 kW for ten minutes in a
  row** (and its level moves the same way): something outside SpotNav drives it, and SpotNav marks it watch-only. This ends by itself 48 hours after the
  battery last showed it. Tibber or Greenely set up in Home Assistant is named as the likely source but
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

When an exported kWh would cost you money, SpotNav 3.0 wants the inverter to stop exporting. "Costs money"
means the spot price plus any credit you get per exported kWh is below zero: with a 5 öre credit, the spot
price must be below −5 öre. In those quarter hours SpotNav would set the site's export limit to 0 kW, and
put back the value it found when the price turns positive again. Your sun still covers the house, the car
and the battery; the inverter only curtails what is left. The battery plan already counts on it: in those
quarter hours it stores the sun first and never plans to sell at a negative price.

**Unless SpotNav's battery control is on (off by default, in testing), this is shadow only: nothing is
written.** The site's diagnostics (`battery.export_curtail`) show
whether a negative-price quarter hour is running, what SpotNav would have written or why it could not, and
per day the kWh you exported at a negative price and what that cost you. The battery plan's nightly review
shows the same figure (`curtail`).

* **Sigenergy** (the Sigenergy Local Modbus integration) is the first brand: the plant's **Grid Export
  Limitation** number. It works in every EMS mode, needs the plant's grid sensor, and is disabled in Home
  Assistant by default. A limit that was never set reads `unknown`; SpotNav would put back the plant's
  maximum output instead, never `unknown`.
* SolarEdge, Huawei, SolaX, Sungrow, Victron and GoodWe also have a local export limit; SpotNav does not use
  them yet.
* Works with or without a battery: without one, SpotNav finds the plant through your grid meter's entity.
* A plant in its own optimiser (Sigen AI mode) or run by another system is only watched, as for the battery.
  Its exported kWh at a negative price are still counted, apart, so you can see what it costs.
* If you or the app change the export limit while SpotNav holds it at 0 kW, SpotNav leaves your value alone
  and does not write it again.
* Beside SpotNav's own battery control: a plant in Remote EMS because SpotNav runs the battery is still SpotNav's
  to curtail. Remote EMS of another app's is not: if the app or a VPP takes Remote EMS while SpotNav curtails,
  SpotNav puts the limit back and only watches. The export limit stays at 0 kW when SpotNav hands the battery back.
* If the site keeps exporting for a few minutes with the limit at 0 kW, the inverter is not obeying it:
  SpotNav would put the value it found back and stop trying.

There is no setting of its own: it runs with the battery control switch, and needs no check of its own (its first
real curtail is the check: the export must stop within a few minutes, or SpotNav puts the limit back and stops
trying).
