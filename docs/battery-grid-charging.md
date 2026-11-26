---
title: Grid charging and the main fuse
description: How SpotNav keeps a home battery's grid charge under the main fuse, also if Home Assistant stops, and how a small fuse can still charge from the grid.
order: 33
section: House and battery
---

# Grid charging and the main fuse

The technical side of a grid charge under [SpotNav's own battery control](home-battery.md#spotnavs-own-battery-control-off-by-default-in-testing).

In short:

- Most batteries keep their last command if Home Assistant stops. So SpotNav caps every grid charge so that no
  phase passes the main fuse even then, with the house at its peak. It learns the house's peaks per phase over the
  first two weeks; until then the cap is cautious.
- On a 16–20 A fuse that cap often leaves little or nothing. Two ways out: a Sigenergy plant that holds each phase
  under its own import limit ([Grid charging on a small main fuse](#grid-charging-on-a-small-main-fuse-sigenergy)),
  or accepting the risk yourself ([Accepting the risk if Home Assistant stops](#accepting-the-risk-if-home-assistant-stops-other-brands)).
- A plant that takes a power target per phase can charge on the phases that have room ([Grid charging per
  phase](#grid-charging-per-phase)).
- While Home Assistant runs, the live headroom under the fuse always comes first, and the chargers before the battery
  unless you put the battery first.

## The grid charge cap and the house's peaks

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

## The plant's own import limit is a total

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

## Grid charging on a small main fuse (Sigenergy)

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

## Accepting the risk if Home Assistant stops (other brands)

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

## Grid charging per phase

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
