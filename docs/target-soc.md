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
vehicle settings. Chargers do not report which car is plugged in, so at a charger more than one car can
charge at SpotNav reads the cars' own plug sensors and positions and asks you when they cannot tell: see
[Which car is plugged in?](vehicle-identification.md). Each car has its own charge target, the same at every charger. Without
a vehicle integration, charge a fixed number of kWh instead.

## How it behaves

- **Stopping.** With a target, the charge stops when the level reaches it, in addition to
  ending with its last period. A target only ever stops a charge; it never extends or restarts
  one. It is checked when a plan is installed, when a period opens, and whenever the reading
  changes.
- **Stale readings.** Cloud-polled car sensors can be an hour old in the middle of a charge.
  When the reading is older than three minutes, SpotNav estimates the level forward from the
  charger's energy register (set or found automatically on the charger) and marks it as
  estimated. A fresh reading always replaces the estimate. Many registers book energy late (an Easee's
  lifetime register can post an hour's charge at once), so a reading is carried forward only from the first
  value the register posts at or after it: energy charged before the reading and booked later is never
  counted on top of it, and energy charged between the reading and that value is left out, so the estimate
  can fall short of the car, never run ahead of it. Until that value comes the estimate stays at the reading.
  A reading taken while the register could not be read yet (a restart before the charger's integration is
  up) is carried forward from the register's first value. When Home Assistant restarts, the car's integration
  writes its last value again, stamped with the start; SpotNav takes that first value with the last reading's
  level as the last reading set again, keeps its time and the register value it is measured from, and the
  energy delivered before and after the restart both count. When the car's sensor only comes back later (its
  integration reloaded) with the value of the last reading, and the charger saw the car plugged in all the
  while, that is the last reading set again too. Otherwise (the car was unplugged, or the charger could not
  say for a while) the value is a new reading and the energy delivered before it is no longer counted: when
  in doubt SpotNav may charge again, never leave the car short. A value that moved is always a new reading.
  A register that starts counting again (a per-session register at a plug-in or a restart, a meter that
  reads 0 while its charger reboots) keeps what it had counted; one back at its old count was a glitch, and
  nothing is counted twice. When the register itself changes (found again, or chosen in the card), counting
  starts again from the new register's first value.
- **When the car reported.** Many car integrations put the car's own report time on the same device: Kia
  and Hyundai's "Last updated at", Renault's "Last battery activity", Polestar's battery data time, VW's
  "Last connected", Ford's "Last refresh", or a generic "Last updated" or "Last communication". When
  exactly one such timestamp sensor is found on the car's device, a reading's age is taken from it instead
  of from when Home Assistant wrote the value, so an hours-old value is never taken as fresh. A report time
  newer than the last reading is a new reading even with the same value. A sensor that names something
  else (a trip, a position, a service date, a charge's end), an unknown or unavailable one, or two that
  match alike are not used, and the age comes from Home Assistant as before. The charger's diagnostics
  show which one was used (`soc.last_reading.time_basis`). The estimate never goes past the car's own charge limit (100 % when the car
  states none). Showing another car (the card's list of the cars at a charger, a site's overview) never
  touches the estimate of the car the charger plans for.
- **An estimate at the target asks the car first.** When only an estimate says the target is reached,
  SpotNav keeps charging and asks the car's integration once to read the car again (the same re-read as the
  app's refresh; a wake-up only for a car that [opted in](#waking-the-car-for-a-reading)). A reading at or above the target then stops the charge as a reading. A
  reading below it lets the charge go on, and the car is asked again only once the estimate has moved
  another percentage point, never on every step of the register. When no reading comes within ten minutes,
  or the car has nothing to re-read, the estimate alone stops the charge once it is 3 percentage points above
  the target. Ten minutes is about the time a small battery takes to gain those 3 points, so the wait costs
  little overshoot.
- **One stop per charge.** A plan installed straight after a target's stop (Auto calculating again) that the
  same stop still satisfies ends on that stop's record: the charge is told complete once.
- **After a charge, before the car reports.** Where the level cannot be carried forward (no register
  then, or one that started again), a target whose charge has ended and delivered, as measured by the
  charger's register or a smart plug's power, at least what the car's last reading needed is not
  planned again on that reading. This needs a charger that reports the car connected, and counts only
  that plug-in's charges recorded for this car; a car found connected after Home Assistant restarted,
  or after the charger could not say for a while (offline), counts from then, since another may have
  been plugged in meanwhile. The status says *Waiting for the car to report its new level after
  the charge*, and SpotNav asks the car's integration once to read it again (the same re-read as the
  app's refresh, never a wake-up). A newer reading plans at once (even the same value, if the car
  really did not take the energy), an unplug ends the wait, and with a departure the need is planned
  again in time to still fit. Energy that was only estimated from the charger's current changes nothing.
  Once the car has ended the charge by itself (below), a period still ahead or a charge control still on
  no longer keeps this wait from starting, and an estimate that says the car is full waits the same way
  instead of planning the last percent or two again and again.
- **The car ends the charge.** When, inside a planned period with the plan's charge on, the car takes
  nothing for five minutes (the charger measures at most 1 A, or an OCPP charger says `SuspendedEV`), the
  car has ended the charge. Load balancing pausing it, the charger holding it back itself
  (`SuspendedEVSE`) and a Start not yet answered are not the car. While load balancing holds the
  plan's charge back for a home battery charging from the grid, a car that refuses two of its battery
  probes in a row (it takes nothing at its minimum current, and the charger says `SuspendedEV`) has
  ended the charge the same way ([load balancing](site-and-load-balancing.md)). If the car's level then says it is full
  for the plan (a reading within 1 percentage point of the target, or of the car's own limit when that is
  lower; an estimate within 3), the plan ends there as a reached target: the charge is stopped, the
  periods still ahead are cleared, the card shows the level it stopped at (*Stopped at 100 % (estimated)*)
  and a charge to the car's own limit is told as *Charging complete: the car is full.* When the level
  then is one nobody read at the end (the estimate from delivered energy, or a reading from before the
  end), the plan waits for the car's level: no period starts the car again, neither the one open nor a
  later one, the status says *The car ended the charge at ~86 % (estimated); waiting for the car's level*
  (without "~" and "(estimated)" on an older reading), and the end is told once, on the estimate:
  *Charging stopped at 86 % (estimated), the target is 91 %.* No departure ends this wait. A reading made
  after the end decides: below the target (by more than the 1 point above) the car is charged as planned,
  at the target the plan ends as above and is told with the car's own level. Unplugging or plugging in
  again, raising the target above the one the car ended at, or a person's Start ends the wait too. (On 14
  October a car full at 100 % read 86 % by the estimate against a target of 91 %, and the next period
  started a car that drew nothing while the status said it charged.) Only a reading made just before the
  end (within three minutes) that is below the target means the car stopped for a reason of its own (its
  own timer, a fault): the charge and the plan stay, the period open then does not start it again, and a
  later period charges as planned. Either way SpotNav asks the car's integration once to read its level
  again (and, while the plan waits for the level, wakes a car that [opted in](#waking-the-car-for-a-reading)); a
  fresh reading below the target is planned again at once.
  The car having stopped by itself is what lets a level within that margin count: no reading or estimate
  ever stops a car that is still drawing short of its target.
- **To the car's own limit.** A target at or above the car's own charge limit (or 100 % when the car
  states none) is the car's to end: SpotNav keeps the charge on within the planned periods and never
  stops it on a reading or an estimate while the car draws, and a car that stops taking current there is
  full, not a fault: the plan ends then, as above.
  The status says *Charging until the car stops at its own limit (100 %)* while it runs. A car still
  drawing when the last planned period ends is let finish: the charge stays on until the car stops by
  itself, at most an hour past the period and never past the departure (*Charging until the car is full
  (at most until 06:40)*). A target below the car's limit still ends with its last period.
- **Unreadable levels never stop a charge and never prevent one.** A sleeping car reports
  nothing; the period's own end remains the guard.
- **Manual Start** is a decision by a person and is not vetoed by a level at or above the
  target.
- A reached target overshoots slightly: the reading that crossed it describes where the car was
  a moment ago. The card shows the level the charge stopped at and how old or estimated it was, and
  the notification says the same: *the target 80 % was reached (the car shows 81 %)* on a reading,
  *Charging stopped at 83 % (estimated), the target is 80 %* on an estimate, and *The car stopped
  charging at 79 %, just below the target 80 %* for a car that ended the charge within a point of it.

## Minimum charge level

Each car can have a minimum charge level, **Minimum charge level** in the card's car settings (off by default, 10-80 %
in steps of 5, set with a slider that stops at the car's charge target). It is the car's own, like its target: the car the charger plans for brings its floor with it to
every charger. While the car's known level is below it, SpotNav charges at once at the full current set (your
amps, capped by load balancing as any charge), whatever the strategy: cheapest periods, the sun and hybrid do not
hold it back, and a charge the plan or the sun runs is taken over at full current. The status says *Charging to the
minimum level (30 %)* while it runs.

- **A person's pause or Stop wins.** A pause you chose, or a Stop, holds the minimum level too: you decided. A
  charge you started is yours.
- **At the floor the strategy takes over**, with no stop and start: a planned period open now goes on with the
  charge, the sun (solar, hybrid) keeps it while its surplus does, and otherwise the charge stops. With a target,
  the plan is made for the rest of the need, from the minimum level to the target. A floor charge that ended at the
  floor starts again only when the level is a couple of percent below it, so a reading that wobbles never cycles
  the charger.
- **It needs the car's level.** A fresh reading, an estimate carried forward from the charger's energy register,
  or a reading the car reported while plugged in counts. With none of these (no level sensor, or only a reading
  from before the plug-in) the minimum level does nothing, and the setting says it needs the car's charge level.
- It never charges past the target or the car's own charge limit: a minimum above either is capped there.
- No notification of its own: *Charging started* and *Charge complete* are sent as for any charge.

## Waking the car for a reading

Many car integrations answer a re-read from their cloud's copy of the car, and a sleeping car's copy can be hours
old: a Kia EV6 that had charged to 100 % read 86 % to SpotNav all night. **Wake the car for a reading** in the
car's settings (off by default, an administrator's setting) lets SpotNav ask the car itself to report at a charge's
end, when the cheap re-read brought nothing fresh (no reading made in the last three minutes):

- when only an estimate says the target is reached, inside the ten-minute wait for the asked reading (a car
  answers a wake in one to three minutes; SpotNav waits at most five for it, then reads the car's entities again);
- when the car ended a plan's charge and the plan waits for its level.

It costs the car's 12 V battery and a request from the maker's limit, so SpotNav wakes a car at most once per
charge end and never within 30 minutes of its last wake, whatever answered; the last wake is stored, so a restart
cannot wake it again sooner. A wake that fails or times out is only noted: SpotNav goes on as without it (the
estimate with its margin). A person's refresh from the app never wakes the car. The charger's diagnostics show each
car's last wake (`vehicle_wake`: when, through which integration, for which charge end, and the result).

The setting is offered only for a car whose integration has a wake SpotNav knows:

| Integration | Wake |
| --- | --- |
| Kia Uvo / Hyundai Bluelink (`kia_uvo`) | `kia_uvo.force_update` with the car's device. It asks every car on that account to report. |
| Tesla Fleet (`tesla_fleet`) | The car's **Wake** button. |
| Teslemetry (`teslemetry`) | The car's **Wake** button. |
| Tessie (`tessie`) | The car's **Wake** button. |

## Vehicle charge limit

If the vehicle's own integration exposes a charge-limit entity, SpotNav shows the limit in the
card's plan settings and plans up to it. An administrator can set it in the card (**Charge limit** in the car's
settings) and any paired SpotNav app can too: the editor is a slider over the range and step the car's
integration takes (50 to 100 % in steps of 10 on a Kia, for example; 1 to 100 % when it does not say). SpotNav
writes at most once a minute per car, and the car takes the new limit when its integration next reads it.

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
integration's own force-update service, so SpotNav does not wake the car (only a car that
[opted in](#waking-the-car-for-a-reading) is woken, and only at a charge's end). A sleeping car keeps
reporting what it last knew, and SpotNav asks at most once a minute per vehicle. Between readings
the level is estimated from the charger's energy register.
