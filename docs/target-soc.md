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

## How it behaves

- **Stopping.** With a target, the charge stops when the level reaches it, in addition to
  ending with its last period. A target only ever stops a charge; it never extends or restarts
  one. It is checked when a plan is installed, when a period opens, and whenever the reading
  changes.
- **Stale readings.** Cloud-polled car sensors can be an hour old in the middle of a charge.
  When the reading is older than three minutes, SpotNav estimates the level forward from the
  charger's energy register (set or found automatically on the charger) and marks it as
  estimated. An estimate stops a charge only once it is a margin above the target. A fresh
  reading always replaces the estimate.
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
