# Heat pumps

SpotNav finds a heat pump by its Home Assistant integration, with no setting, and reads what the pump reports. It
writes no setting, no offset and no setpoint on any heat pump. The one thing it can switch is a **relay** that lowers
the pump's curve, and only when you turn on **Styr värmen** (see [below](#spotnavs-own-heat-control-relay-off-by-default)).
What it reads is shown in the diagnostics (`heat_pump`).

## Which pumps are read

| Pump | Integration | Notes |
| --- | --- | --- |
| NIBE F series (F730, F750, F1155, SMO 40, VVM ...) | NIBE Heat Pump (`nibe_heatpump`, local) | Its entities are disabled by default: enable the readings under "What is read" to have them read. |
| NIBE S series (S1155, S320, VVM S ...) | NIBE Heat Pump (`nibe_heatpump`, local) | As above. |
| NIBE (F series, SMO 20) | myUplink (`myuplink`, cloud) | Read through NIBE's cloud. |
| CTC (EcoZenith, EcoLogic, EcoAir ...) | CTC Heat Pump (BMS) (`ctc_bms`) or CTC Local Easyconf (`ctc_ecozenith`) | Local Modbus. |
| Panasonic Aquarea | HeishaMon (`aquarea`, MQTT) | Needs the HeishaMon board. The HeishaMon YAML package is not read. |

## What is read

Where the pump has it: the outdoor, supply, calculated supply (NIBE 43009), return, room and hot water temperatures,
the degree minutes, the
compressor's speed and whether it runs, the immersion heater's power or state, and whether the pump is heating the
house or the hot water now. A reading that is `unknown` or `unavailable`, belongs to a disabled entity, or is out of
any plausible range (NIBE's "no sensor" -3276.8, for example) is left out, and the diagnostics say why.

SpotNav also lists the controls a pump offers (heat curve offset, setpoint, SG Ready through the integration, extra
hot water, compressor limits) with their entities and ranges. It only lists them.

## The pump's own price control

When the pump runs its own price control (NIBE Smart Price Adaption, CTC's SmartGrid or spot price mode), or
EffektGuard is set up, the diagnostics mark the pump as **watch only**: a later SpotNav heat control will leave such
a pump to it and only watch.

## The heat card

The heat card (`type: custom:spotnav-heat-card`, with `site:` or `charger:`) has a header and three tabs. The header
says after the card's name what the heating does now: **Värmer 2,1 kW**, **Varmvatten 2,9 kW** or **Vilar** from the
heat pump's own facts or its meter, and **Sänkt till 06:00** or **Höjd till 15:00** while SpotNav's own control holds
the heat down or up. Before SpotNav controls anything, **Test** stands first (tap it for what that means). The **i**
opens the details, the gear (administrators) the settings.

* **Now**: the dial for the wanted temperature with its band, the coldest room and the outdoor temperature, **Hemma**
  or **Borta**, what SpotNav would do (or does) and what the relay does, a thin strip of **the plan's next twelve
  hours** (a cell an hour: a lowering, a pre-heat or raise, the cooler night, hot water as a dot) with the same in
  words under it, and today's heat, hot water and cost. The strip ends at midnight until tomorrow's prices are in.
  The sensors, the zones and how the pump is controlled are in the gear's settings.
  **Borta** holds the house **3 °C below the wanted temperature** (never below 16 °C) until two hours before the
  return time, then warms it up. The gear's **Setback when away** (*Sänkning när borta*) sets those degrees, 1 to 6
  in half degrees; the warm-up before the return is the same.
* **Plan**: the day's chart: the rooms, the comfort band, the setpoint, SpotNav's lowerings (what it did solid, what it
  plans lighter), the relay, hot water and the car. The arrows step back through past days (tap the day to choose
  one); this tab shows days only. Tap or point at the chart for a time's figures.
* **Use**: bars per hour, day or month (the period picker: Day, Month, Year), in **kWh or money** (the switch at the
  right, remembered). Each bar is stacked by **what is actually measured**: the **heat pump** (its meter less the hot
  water), **hot water** (the meter's plateaus of about 3 kW, on the days Home Assistant still has in 5-minute detail;
  on older days it stays in the heat pump's part and the card says for how many days it is split out), each **zone
  with a meter of its own** by its name (an air-to-air heat pump; the four largest), and **other heating** (every
  other heat counted: further zones, a meter moved to Heat in the house card's pie). A zone without a meter is in no
  bar: nothing is guessed. The parts in the legend are toggles with their sums (remembered). The headline is **what
  SpotNav saved on heat** over the period, the same figure as *Heat* in the house card's Earnings tab; a period
  without one is headed by the heat's total.

Plan and Use share the period: a day chosen in one is the day in the other, and a month or year chosen in Use is
still there after a look at Plan. The tab, the unit and the hidden parts are remembered per card in the browser.

**The details** (the **i**) use the same period picker and show everything at once: the cost with what SpotNav's plan
would have saved hatched on top, the use in figures (the immersion heater too, where its power is read), **degree
days** and kWh per degree day with the monthly trend, **comfort** (the coldest room and the hours under the band),
**what SpotNav would have done** in shadow, a day's review of the relay beside SpotNav's plan, **the house model**
(how far its forecast of the heating was from the meter, in kW, over the reviewed days) and a day's hot-water runs.

A wall screen can show one tab only, without the tab row: `tab: now`, `plan` or `use` (the card editor's **Show only
tab**). It is a setting of this card, not of SpotNav. In a box of fixed height the dial, the chart or the bars take
the box's spare height and the explaining lines and notes go. Every tab fits a 650×520 box with the tab row shown.

## The heat's figures: the heat pump and the zones

The heat pump's meter (found by itself, or chosen in the site settings) is the heat card's own measure: its
hot-water split and runs, the pump state, the degree-day figure and its trend, and SpotNav's control all read it alone.

Electricity used for heat elsewhere counts as heat too: the chosen **zones** (electric floor heating, an air-to-air
heat pump) with a meter of their own on the thermostat's device (an energy sensor, else a power sensor; no setting),
and any Energy dashboard meter moved to Heat in the house card's pie. Each is counted once (a zone whose sensor is the
heat pump's meter, or nested inside it, is left out), and a zone without a meter is listed in the diagnostics only.

* **Today** on the heat card: *heat* holds the zones (and says how much as *of which zones*); *hot water* stays the
  heat pump meter's split, and the cost counts the zones at each period's price.
* **The Use tab and the details**: each bar, the total and the cost hold the zones in the hours the heat pump's meter
  has, a zone with its own meter as a part of its own and the rest as *other heating*, with *Of which zones* beside
  the total in the details. The zones count from the day their meter has readings, so the use takes a step
  up there; the history says from which day. The **degree-day** figure and its trend stay the heat pump's alone, so a
  zone added does not look like a worse house.
* **The house card**: its flow's Heat is the same sum, with its own pie where it holds more than one source
  ([house card](house-card.md#heat-in-the-flow)).

## Indoor sensors and their offsets

In the heat settings (the settings popover's **Heating** section) you choose one or more indoor temperature sensors.
The room **closest to too cold** rules, not the mean: when it is below the comfort band (the wanted temperature less
half a degree), SpotNav lowers nothing. A sensor that rises fast while the others stay flat (a wood stove, the sun
through a window) is left out while it lasts.

Each chosen sensor has an **offset** against the house temperature, from −3 to +3 °C in half degrees (0 by default),
edited on its own row under the sensors, like a zone's offset. Use it for a room that is meant to be cooler (an
upstairs kept at −1.5) or for a sensor that reads wrong (one that shows half a degree too warm: +0.5). SpotNav then
compares each room's reading less its offset with the house's band, both in the coldest-room rule and in the
comfort figures of the day's review and the heat history. The card still shows each room's own reading.

## SpotNav's own heat control (relay, off by default)

With the heat pump's lowering input wired to a relay in Home Assistant (NIBE's external contact set to shift the curve
down, or an SG Ready pump's first contact), SpotNav can switch that relay itself on its own heat plan, the same plan
the heat card's day chart shows. **It is off by default.** The switch, **Styr värmen**, is in the **Heating** section of
the settings popover (administrators only) and is shown when the control path is **Relay** or **SG Ready**. Off, the
relay is left alone and SpotNav only shows what it would do. Level C/D (a setpoint or a curve offset written to the
pump) is not part of this.

**Open relay = the pump's normal curve.** That is the fail-safe state, and every way out ends there: the switch turned
off, a block or a pause (below), a safety reason, another relay chosen, SpotNav unloaded, and Home Assistant stopping.
A relay SpotNav could not open at a stop is opened at the next start before anything else.

**First a check.** Switching on starts a short check, again after the relay entity changes: at a moment when the pump
is heating, SpotNav closes the relay and looks for an effect within 20 minutes (the heat pump meter's power drops to
at most 70 % of what it was, or, where the pump reports it, the calculated supply temperature falls by 1.5 °C or more;
a NIBE showed 40.3 → 35.7 °C), then opens it and looks for the return within 30 minutes. A moment that cannot be
judged (a hot-water run starts, the readings go, a room gets cold) is tried again at least an hour later, at most
three times a day. Without a heat pump meter and without a calculated supply reading nothing can be judged, and the
line under the switch asks you to choose the meter. A failed check stops control; turn the switch off and on to try
again after checking the wiring.

**The rules** (fixed, no settings):

* SpotNav lowers only where its plan lowers (the dear hours, the hours the import target or the fuse is tight, or
  while the house is too warm: below), and each lowering lasts at least **45 minutes** (no quarter-hour blips), at most **3 hours in a row**, with at least
  **1 hour** of normal heat between lowerings and at most **6 hours a day**.
* **Too warm indoors lowers too, whatever the price.** When the coldest counted room (its offset counted) has been
  above the comfort band (the wanted temperature plus half a degree) for **an hour**, the plan lowers the running hour
  and the next, until the room is back **0.2 °C** inside the band; then the lowering ends once its 45 minutes have
  passed. A room whose own sensor rises fast (0.6 °C in half an hour: a sensor warmed by sun or a stove) does not
  count as warm while it rises, also with a single sensor. All the other rules hold, and an hour both dear and warm
  is one lowering, not two. The day's band shows these hours as lowered with the reason **för varmt inne** (too warm
  indoors), as "would lower" while the switch is off. The hour above the band is counted in memory and starts again
  after a restart.
* Never at **−15 °C or colder** outdoors.
* **Released at once** when a counted room (its offset counted) falls below the comfort band, when the plan or the
  outdoor reading is missing or old (and nothing starts again until they have been good for 10 minutes), or when SG
  Ready's second contact is on by someone else.
* At most 6 switchings a day (both SG Ready contacts together).
* On the relay path pre-heating stays planning only: one relay can lower the curve, not raise it. SG Ready with both
  contacts can raise as well (below).

### SG Ready: the four states and what SpotNav uses

An SG Ready heat pump has two inputs, A and B. An electrician wires a relay that Home Assistant can switch to each:
one to input 1 (**A**, the **Control output** in the settings) and one to input 2 (**B**, the **Second contact**).
The pair gives four states, written (A, B):

| State | A | B | What the pump does | SpotNav |
|---|---|---|---|---|
| Blocked or lowered | closed | open | blocks the compressor or lowers its curve, as set in the pump | the plan's lowered hours |
| Normal | open | open | its own curve | every other hour, and every way out |
| Recommended raise | open | closed | raises its curve or its hot water, within its own limits | the plan's pre-heat hours |
| Forced raise | closed | closed | heats as hard as it may | **never** |

**(1,1) never happens**, not even for a moment: SpotNav switches through the normal state, opening one contact and
reading it back as open before it closes the other. A contact that does not open in time keeps the other one open.
With only A chosen, SG Ready works like the relay path: it lowers, never raises.

**When SpotNav raises** (with the switch on and the check passed): in the plan's **pre-heat** hours (the cheap hour
before a lowered run, when it costs at most 85 % of the run's mean price) and in the warm-up hours before the return
from away. Each raise lasts at least **30 minutes** and at most **3 hours in a row**, with at least an hour between
raises and at most **6 hours** in 24 hours, and a raise starts only while the day's switchings leave room for the
lowering that follows it. **Never** into a warm house: no raise while any room (its offset counted) is at or above
the comfort band's upper edge or the too-warm rule is lowering, and a raise ends at once when a room reaches the
edge. Never during the cooler night or while away, except the warm-up before the return. Nothing raises while the
plan or the outdoor reading is missing or old.

**The raise is checked on its first runs**, like the lowering: within 20 minutes the calculated supply temperature
should rise by 1 °C, or the heat pump meter's power by a fifth. A pump whose supply does not rise is noted as **raise
not observed** in the diagnostics (`heat_settings.control.raise`); SpotNav then stops raising and keeps lowering,
without a note on the card. The power alone never fails the check (the compressor starts and stops by itself), and
it passes only after two raises that both show it. Turning the switch off and on tries the raise again.

The day chart shows SpotNav's raises as solid warm columns (**SpotNav höjde**) and the planned ones lighter
(**Planerad höjning**, or **Skulle höja** while the switch is off), and the line under the switch says **Styr: höjer
värmen till …** while a raise runs.

**Every write is read back**: the relay must show the new state within 90 seconds. A switching that fails or is not
seen pauses control for the rest of the day; an opening is tried again every five minutes until the relay reads open.

**Someone else's change wins.** If the relay is switched by anyone but SpotNav (by hand, from another automation), SpotNav
gives it up at once, writes nothing, and pauses until the next day. While an enabled automation of yours references
the relay (the SpotNav relay blueprint, for example), control does not start, and one note says which one to turn off.
SpotNav never turns an automation off. Another heat optimiser (EffektGuard) or the pump's own price control also keeps
control from starting.

**What you see**: the line under the switch (off; checking; controlling and until when; or what it waits for), the
heat card's header saying what the control does now after its name (**· Sänkt till 19:00**, **· Höjd till 15:00**
for SG Ready's raise, else what the heat pump does) with no **Test** before it, and SpotNav's own lowerings as solid
columns in the Plan tab's chart (**SpotNav sänkte**), beside the plan's lighter ones, the rooms and the setpoint. Tap
or point at the chart for a time's figures. The diagnostics (`heat_settings.control`) keep the switchings, the check's
results and why each decision was made.
