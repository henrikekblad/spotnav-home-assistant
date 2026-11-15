# Home batteries

For anyone with a home battery and a planner such as Predbat or EMHASS: how SpotNav signals a grid charge, and why it never controls the battery.

SpotNav tells your home battery planner when the car is charging from the grid. It does not control
the battery itself. SpotNav 3.0 also plans the battery in shadow, as a benchmark you can compare with what the battery really
does; that part only watches and is described in [The battery plan](battery-plan.md) and the
[house card](house-card.md).

## Why SpotNav does not write to batteries

Battery integrations differ in which settings they expose, and firmware does not always obey them.
On some Sigenergy systems, for example, the discharge limit is ignored in AI mode, and a "hold"
done through the discharge cut-off SoC can make the inverter import several kW from the grid, next
to your main fuse. A planner such as Predbat or EMHASS already owns the battery, and two writers on
one battery fight each other. So SpotNav only publishes a signal, and the planner or an automation of
yours acts on it.

One limit to know before any controller (SpotNav's own, when it comes, or yours) writes to a Sigenergy plant:
Sigenergy has no dead-man. If Home Assistant stops (not restarts) during a command, the plant keeps that command
until Home Assistant is back and the controller restores what it found (SpotNav does that first after a
start).

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
notices the others and only watches. Today SpotNav writes to no battery at all, so every battery is only watched; the
detection matters for the opt-in that may come later.

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

**Today this is shadow only: nothing is written.** The site's diagnostics (`battery.export_curtail`) show
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
* Beside SpotNav's own battery control, which is for later (today SpotNav runs no battery): a plant in Remote EMS
  because SpotNav runs the battery would still be SpotNav's to curtail. Remote EMS of another app's is not: if the
  app or a VPP took Remote EMS while SpotNav curtailed, SpotNav would put the limit back and only watch. The export
  limit would stay at 0 kW when SpotNav hands the battery back.
* If the site keeps exporting for a few minutes with the limit at 0 kW, the inverter is not obeying it:
  SpotNav would put the value it found back and stop trying.

There is no setting for this: it will come with the battery control opt-in.
