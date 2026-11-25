---
title: No export at negative prices
description: SpotNav sets your inverter's export limit to zero while selling electricity costs money, and puts it back afterwards. Which brands, the switch, and what it never does.
order: 52
section: Solar and forecasts
---

# No export at negative prices

When an exported kWh costs you money, SpotNav can tell the inverter to stop exporting. "Costs money" means the spot
price plus any credit you get per exported kWh is below zero: with a 5 öre credit, the spot price must be below
−5 öre.

In those quarter hours SpotNav sets the site's **export limit** to zero. When the price turns positive again it puts
back the value it found. Your sun still runs the house, the car and the battery. The inverter only curtails what
is left. The [battery plan](battery-plan.md) already counts on it: in those quarter hours it stores the sun first and
never plans to sell.

**In testing.** This is new in 3.0 and has only been tried on a Sigenergy plant so far. For now it writes only on
a [beta tester's](beta-testing.md) installation; see [Brands](#brands).

## The switch

**Settings → Solar → No export at negative prices.** It is off by default, and only an administrator can change it.
It applies to the whole site.

- The row is shown only when SpotNav finds an export limit it can run in Home Assistant (see [Brands](#brands)).
- It works with or without a home battery. Without one, SpotNav finds the inverter through your grid meter's
  entity.
- It is its own switch. Turning on **Battery control** does not turn it on, and it does not wait for the battery's
  hardware test or control code.
- A Sigenergy site where SpotNav's battery control was already on and writing got the switch turned on once, by
  the update, so nothing stopped. You can turn it off.

While the switch is off, SpotNav still counts what you export at a negative price. The row shows what that cost
you in the last 30 days, so you can see what the switch would be worth.

## Brands

SpotNav writes one number in the inverter's own integration, and for some brands a switch that turns the limit on.
The entities must exist and be **enabled** in Home Assistant. The row says so when the limit's entity is disabled,
with a button that opens it.

| Brand | Integration | Entities SpotNav uses | Notes |
|---|---|---|---|
| Sigenergy | Sigenergy Local Modbus (HACS, TypQxQ) | Number **Grid Export Limitation** | Disabled by default: enable it. Needs the plant's grid sensor. An export limit must already be set, see below. |
| Victron | Home Assistant's Victron GX integration (`victron_gx`, `victron_mqtt`) | Number **ESS max feed-in power** | −1 means no limit; SpotNav puts −1 back exactly. It also stops the battery feeding in. Not the community Modbus integration. |
| Sungrow | mkaiser's Sungrow SHx Modbus package | Number **Export power limit**, switch **Export power limit**, sensor **Export power limit min** | Needs the Sungrow meter. Not started where the inverter's smallest limit is above 0. It also stops the battery exporting. |
| Solis | `solis_modbus` (HACS, Pho3niX90) | Number **Backflow Power**, switch **Grid feed in power limit switch** | Needs the meter or CT. Not started while the inverter's Flexible Export is on. |
| GoodWe | Home Assistant's GoodWe integration, or the HACS one (mletenay) | Number **Grid export limit**; with the HACS integration also switch **Grid export limit switch** | With Home Assistant's own integration SpotNav cannot turn the limit on: it works only if export limiting is already enabled in the inverter. |
| SolaX | `solax_modbus` (HACS, wills106) | Number **Export Control User Limit** | A setting in the inverter. No switch. |

None of these but Sigenergy has run on a real inverter yet. For every brand in the table:

- **Beta testers only for now.** Until a brand has been seen to work on a real plant, SpotNav writes its export
  limit only on an installation where **Beta tester** is ticked (Settings → Support). Elsewhere the row says so and
  only counts.
- **Solis, GoodWe and SolaX: a check first.** Nothing from these makers states that 0 means "no export" rather
  than "no limit". So before the first real use SpotNav checks it once by itself: the next time your site exports
  at least 1 kW, it sets the limit to zero for a few minutes, sees that the export stops, and puts the limit back.
  Only after that does it act on negative prices. The check costs a few minutes of export.

**Sigenergy and a limit that was never set.** If no export limit has ever been set on the plant, the number reads
`unknown`. SpotNav could lower it but could not put "no limit" back, and it never writes the plant's maximum in its
place, since that could overwrite a cap your grid operator requires. So it does nothing, and the row asks you to
set an export limit in the Sigenergy app first, at the most you are allowed to export.

**Not possible:** Fronius and Kostal only have a limit on the inverter's whole output in Home Assistant. Setting
that to zero would feed the house from the grid, so SpotNav never uses it. Enphase, Ferroamp and Marstek have no
export limit in their integrations.

**Not yet:** SolarEdge, Huawei, Tesla, Deye and Sunsynk, FoxESS, EcoFlow, Solinteg and Growatt have an export limit
of some kind that SpotNav does not use yet.

## What SpotNav does and does not do

- **It puts back exactly what it found.** The value first, then the switch. A switch that was off is switched off
  again. It writes the snapshot to disk before the first write, and restores it first after a restart.
- **It leaves your changes alone.** If you, the inverter's app or an installer change the limit or its switch
  while SpotNav holds it at zero, SpotNav stops and does not write again that day.
- **It checks that it works.** After two minutes the export must have stopped. If the site keeps exporting,
  SpotNav puts the limit back and stops. If the site starts importing instead, more than the battery and the
  cars take, the "limit" is holding back the whole inverter: SpotNav puts it back and stops. In both cases the
  row says so; turn the switch off and on to try again.
- **It needs a signed grid reading.** Without the site's grid power with a direction, SpotNav could not see whether
  the limit works, so it does not start. A site that measures phase currents needs the meter's total grid power.
- **Short gaps are bridged.** If the price turns positive for at most half an hour between two negative
  stretches, the limit stays at zero, unless the battery plan wants to sell in that gap. This saves writes to the
  inverter for a small loss.
- **At most six stretches a day.** Each stretch is two writes to the inverter (four with a switch). SpotNav
  starts at most six in a day.
- **A plant in its own optimiser is only watched.** Sigenergy's AI mode, or a plant another system runs, is left
  alone, as for the [battery](home-battery.md#when-another-system-runs-your-battery). Its export at negative prices is
  still counted.
- **Beside SpotNav's battery control.** A plant in Remote EMS because SpotNav runs the battery is still SpotNav's to
  curtail. If another app takes Remote EMS while SpotNav curtails, SpotNav puts the limit back and only watches.

## If something goes wrong

Turn the switch off: SpotNav puts the limit back before the setting is saved. If Home Assistant was down, check
the limit in the inverter's own app and set it back to your value. A limit left at zero only costs you export
income; it never adds import.

The site's diagnostics show what happened: `battery.export_curtail` (the price now, what would be or was written,
and per day the kWh exported at a negative price and what they cost), `battery_control.export` (the limit's own
state) and `battery_control.export_gate` (the day's stretches, the check, the first curtail on each firmware).
