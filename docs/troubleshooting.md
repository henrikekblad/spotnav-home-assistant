# Diagnostics and troubleshooting

## Diagnostics

Download diagnostics from **Settings, Devices & services, SpotNav**, the entry's menu, then
**Download diagnostics**.

![The entry's menu with Download diagnostics](images/diagnostics.png)
 The file has the entry's configuration, price data state, the planner
and controller state, and for a site the measurements, their ages and the load-balancing
decisions. The webhook id and OCPP charge point id are redacted, and no webhook or pairing URL is
included, so the file is safe to attach to a public bug report.

Useful entities on each charger: *Auto plan state*, *Auto execution state*, *Next planned
charging start*, *Planned cost* and *Auto settings revision*. On a site: *Capacity state* and one
*Proposed current* sensor per charger.

## Setting up

- **"No supported charger was found."** The list of devices was empty. Set up the charger's own
  Home Assistant integration first, then add the charger here, or choose **Manual**. See [Set up SpotNav](setup.md#when-detection-finds-nothing).
- **"SpotNav cannot start or stop this charger. It can only be read."** The charger's integration
  is known but has no control SpotNav can use. See [supported hardware](supported.md).
- **"The charger did not answer, so SpotNav cannot tell how its current is set."** An OCPP charge
  point is asked how its current can be set, and it was not connected. Wait until it is connected
  and submit again, or choose how yourself.
- **"The charger's own control is still on."** The charger's own smart, solar or load-balancing mode
  is on. Turn it off in the charger's integration or app and continue, or choose to continue anyway.
- **"This charge control is already used by another SpotNav charger."** A charge control or current
  number can belong to only one SpotNav charger.
- **A new charger was not added to the site.** Only a charger whose wiring SpotNav can tell (three
  phases and exactly one measured-current source on its device) is offered to the site. Add it from
  the site's **Configure**.
- **The site needs its meter chosen.** When no grid meter is found, pick the entities yourself or
  skip and finish later under the site's **Configure**. See
  [Set up SpotNav](setup.md#when-the-meter-is-not-found).

## Charging

- **OCPP entities are unavailable after a restart.** Give the charger time to reconnect.
- **The wrong connector is used, or the current is not applied.** See
  [OCPP chargers](ocpp.md#which-connector). Choose the Charge Control switch for the outlet you
  use under the integration's **Configure** action.
- **A remote start or stop is rejected.** There may be no active transaction, or the connected
  vehicle is not requesting energy.
- **Nothing is planned.** The card lists what is missing, for example the price area or phases.
  While tomorrow's prices are not published, SpotNav buys what the deadline requires now and plans the rest
  later.
- **The plan is proposed but not applied.** The card shows a newer proposal until it has been
  installed; check *Auto execution state* and whether automatic charging is paused.
- **Prices are stale or missing.** Prices come from the public SpotNav Relay. If the relay is unreachable, the last successful
  fetch is used and the card says the prices are stale. No token or account is involved.

## Vehicles

- **A vehicle needs a decision.** A device reports several battery readings or charge limits. Open
  the repair under **Settings, System, Repairs** and choose which reading to use.
- **The charge-level sensor is wrong.** In the card's vehicle settings choose another sensor, or set it back to automatic.
  The services `dismiss_vehicle`, `undismiss_vehicle` and `unconfirm_vehicle` do the same by device id.

## Site

- **`invalid_measurements`.** The chargers' measured currents together exceed the site's total on
  a phase. Usually a wrong sensor, a sensor mapped twice, or the wrong phase.
- **Load balancing cannot be turned on.** The card says why: no commandable charger, a
  measurement that is missing, or a charger that belongs to more than one site.
- **Measurements are old.** Raise *maximum measurement age* only if the sensor really updates that slowly.

## Phone and pairing

- Internet addresses need HTTPS. Plain HTTP works only for private local addresses and works only while the phone
  is on that network.
- A pairing request expires after five minutes. Start again on the phone.
- If a webhook id has leaked, remove the charger and add it again to generate a new one.
