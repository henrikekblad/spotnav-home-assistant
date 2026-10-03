# Home batteries

SpotNav tells your home battery planner when the car is charging from the grid. It does not control
the battery itself.

## Why SpotNav does not write to batteries

Battery integrations differ in which settings they expose, and firmware does not always obey them.
On some Sigenergy systems, for example, the discharge limit is ignored in AI mode, and a "hold"
done through the discharge cut-off SoC can make the inverter import several kW from the grid, next
to your main fuse. A planner such as Predbat or EMHASS already owns the battery, and two writers on
one battery fight each other. So SpotNav only publishes a signal, and the planner or an automation of
yours acts on it.

## The signal

Two binary sensors, both named "Charging from the grid":

| Entity | Where | On when |
| --- | --- | --- |
| `binary_sensor.<charger>_charging_from_the_grid` | each charger | the charger is charging and the energy is meant to come from the grid |
| `binary_sensor.<site>_charging_from_the_grid_site` | each site | any charger of the site has its own sensor on |

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
  - binary_sensor.<site>_charging_from_the_grid_site
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
        entity_id: binary_sensor.<site>_charging_from_the_grid_site
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
        entity_id: binary_sensor.<site>_charging_from_the_grid_site
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
