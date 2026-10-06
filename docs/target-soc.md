# Target state of charge

Instead of a fixed number of kWh, a charger can plan from a target charge level, for example
"80 % by 07:00". SpotNav then works out the energy the car needs from the wall, including
charging losses.

## What it needs

- A **vehicle** for the charger, with a charge-level sensor and the battery capacity (kWh).
  Vehicles are detected from Home Assistant devices that report a battery percentage and, on the
  same device, a distance (range) sensor. The [supported hardware page](supported.md) lists the
  integrations the detection was checked against; if a
  device reports several battery readings SpotNav ranks them by what the integration calls them:
  health, target, limit, 12 V, arrival and predicted readings are never the state of charge, and
  a "usable" level only loses to a plain one. If exactly one reading is left it is used;
  otherwise SpotNav will not guess and raises a repair asking you to choose which one is the
  state of charge (or to say the device is not a vehicle). A choice you confirmed always wins.
  Detection re-runs after setup and whenever entities are added or changed, so cars whose
  integration creates entities late (for example after a cloud connection comes up) are picked up. Capacity
  and consumption are set per vehicle in the card.
- The charger reads the car's own level when the charger reports it, and otherwise the
  vehicle's sensor.

### Several vehicles

Several vehicles can be detected, and you choose which one the charger plans for, in the card's
vehicle settings. SpotNav does not detect which car is plugged in, because chargers do not report
it: change the choice when a different car is connected. Without a vehicle integration, charge a
fixed number of kWh instead.

## How it behaves

- **Stopping.** With a target, the charge stops when the level reaches it, in addition to
  ending with its last period. A target only ever stops a charge; it never extends or restarts
  one. It is checked when a plan is installed, when a period opens, and whenever the reading
  changes.
- **Stale readings.** Cloud-polled car sensors can be an hour old in the middle of a charge.
  When the reading is older than three minutes, SpotNav estimates the level forward from the
  charger's energy register (set or found automatically on the charger) and marks it as
  estimated. An estimate stops a charge only once it is a margin above the target. A fresh
  reading always replaces the estimate. A reading taken while the register could not be read yet
  (a restart before the charger's integration is up) is carried forward from the register's first
  value. A value written while Home Assistant starts (up to ten minutes after) that equals the last
  reading is that reading set again, not a new one: the estimate goes on from it. The same value
  reported at any other time is a new reading. When the register itself changes (found again, or
  chosen in the card), counting starts again from the new register's first value.
- **After a charge, before the car reports.** Where the level cannot be carried forward (no register
  then, or one that started again), a target whose charge has ended and delivered, as measured by the
  charger's register or a smart plug's power, at least what the car's last reading needed is not
  planned again on that reading. This needs a charger that reports when the car was plugged in, and
  counts only that plug-in's charges recorded for this car (or, with only one car known, charges that
  recorded none). The status says *Waiting for the car to report its new level after
  the charge*, and SpotNav asks the car's integration once to read it again (the same re-read as the
  app's refresh, never a wake-up). A restart does not end the wait: the value set again as Home
  Assistant starts is the old reading. A newer reading plans at once (even the same value, if the car
  really did not take the energy), an unplug ends the wait, and with a departure the need is planned
  again in time to still fit. Energy that was only estimated from the charger's current changes nothing.
- **To the car's own limit.** A target at or above the car's own charge limit (or 100 % when the car
  states none) is the car's to end: SpotNav keeps the charge on within the planned periods and never
  stops it on a reading or an estimate, and a car that stops taking current there is full, not a fault.
  The status says *Charging until the car stops at its own limit (100 %)* while it runs. A car still
  drawing when the last planned period ends is let finish: the charge stays on until the car stops by
  itself, at most an hour past the period and never past the departure (*Charging until the car is full
  (at most until 06:40)*). A target below the car's limit still ends with its last period.
- **Unreadable levels never stop a charge and never prevent one.** A sleeping car reports
  nothing; the period's own end remains the guard.
- **Manual Start** is a decision by a person and is not vetoed by a level at or above the
  target.
- A reached target overshoots slightly: the reading that crossed it describes where the car was
  a moment ago. The card shows the level the charge stopped at and how old or estimated it was.

## Vehicle charge limit

If the vehicle's own integration exposes a charge-limit entity, SpotNav shows the limit in the
card's plan settings and plans up to it. The app can also set it; the card only shows it.

The limit is a number, or a percent picker (a select with options
from 50 to 100); the AC limit is preferred over the DC limit, and discharge (V2L), minimum,
profile, solar and share settings are never taken as the limit. A limit that the integration
enforces itself rather than the car (a soft limit with its own on/off switch) is ignored. A
read-only "target" sensor is used to plan up to the car's ceiling but cannot be set. When a
vehicle still reports several limits SpotNav asks you (as a repair) which one a change should
set. The battery capacity is read from an entity named capacity or size (kJ and MJ are
converted); remaining, available or added energy is never taken for it.

Refreshing a vehicle (from the app) re-reads its Home Assistant entities: SpotNav only asks Home
Assistant to update them, which is a request to the vehicle's own integration, and it never calls an
integration's own force-update service, so SpotNav does not wake the car. A sleeping car keeps
reporting what it last knew, and SpotNav asks at most once a minute per vehicle. Between readings
the level is estimated from the charger's energy register.
