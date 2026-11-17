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

* SpotNav lowers only where its plan lowers (the dear hours, or the hours the import target or the fuse is tight), and
  each lowering lasts at least **45 minutes** (no quarter-hour blips), at most **3 hours in a row**, with at least
  **1 hour** of normal heat between lowerings and at most **6 hours a day**.
* Never at **−15 °C or colder** outdoors.
* **Released at once** when a counted room (its offset counted) falls below the comfort band, when the plan or the
  outdoor reading is missing or old (and nothing starts again until they have been good for 10 minutes), or when SG
  Ready's second contact is on.
* At most 16 relay switchings a day.
* Pre-heating stays planning only: a relay can lower the curve, not raise it.

**Every write is read back**: the relay must show the new state within 90 seconds. A switching that fails or is not
seen pauses control for the rest of the day; an opening is tried again every five minutes until the relay reads open.

**Someone else's change wins.** If the relay is switched by anyone but SpotNav (by hand, from another automation), SpotNav
gives it up at once, writes nothing, and pauses until the next day. While an enabled automation of yours references
the relay (the SpotNav relay blueprint, for example), control does not start, and one note says which one to turn off.
SpotNav never turns an automation off. Another heat optimiser (EffektGuard) or the pump's own price control also keeps
control from starting.

**What you see**: the line under the switch (off; checking; controlling and until when; or what it waits for), the
heat card's header chip **Styr värmen** instead of "Test" while it runs, and SpotNav's own lowerings as solid columns
in the day's chart (**SpotNav sänkte**), beside the plan's lighter ones, the rooms and the setpoint. Tap or point at
the chart for a time's figures; tap its heading to fold it away (each browser remembers that per site). The diagnostics (`heat_settings.control`) keep the switchings, the check's
results and why each decision was made.
