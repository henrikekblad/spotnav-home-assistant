# The SpotNav card

The card is the day-to-day view of one charger: the price graph, the charging plan, the buttons
and all of the settings. One card shows one charger. [Add it](setup.md#3-add-the-card) from the
card picker, and add one card per charger.

![The SpotNav card with a planned band on the graph, the plan and the buttons](images/card-hero.png)

Reading is open to every Home Assistant user. Changing anything needs an administrator; other
users see the values and a note that only administrators can change them.

## Header

The header shows the charger's name and two icons.

- **About this card** (the information icon) opens **What this charger can do**: automatic price
  planning, dynamic current limit, load balancing, and vehicle state of charge and target, each
  marked available or unavailable for this charger. An item that is unavailable here cannot be
  switched on from the card. The list is taken from what the integration reports for the charger.
- **Card settings** (the gear icon) opens [the settings popover](#the-settings-popover).

When something needs attention, a banner says so: a notice in a neutral colour, or a message in
red when something must be fixed before charging can be planned. The banner counts the items to
review and opens the full list with the integration's own wording.

## The price graph

The graph shows the price of every interval, in the price area's currency per kWh.

- One horizontal axis is one local day, 00:00 to 24:00. Today is drawn over tomorrow, and an
  earlier day is subdued. Tomorrow's prices appear once they are published, usually in the
  afternoon.
- Bars cheaper than the day's average are marked as cheap and dearer ones as expensive. A line
  marks now, and the current interval is highlighted.
- The plan is drawn on top of the prices. **Scheduled** bands are the charging periods Home
  Assistant has installed and will follow. **Proposal** bands are a newer plan that is ready but
  not installed yet.
- A summary line gives the highest price, the lowest price and the price now.
- Touch or hover an interval to read its price. With the keyboard, the arrow keys step through the
  intervals, Home and End jump to the ends, and Escape clears the selection. Times follow the
  price area's time zone, and the autumn hour that repeats is shown with its offset.

## The plan

Under the graph the card lists the charging periods: **Cheapest charging periods** for a
proposal, or **Scheduled charging periods** for the plan that is installed. Each period shows its
day and time, and a period that is running now is marked. The card also states the plan's energy
and cost and, when the vehicle's consumption is known, the distance the charge adds.

A plan that had to buy some energy before tomorrow's prices were published is flagged, because part
of it is charged without published prices.

## The buttons

A row of four cells sits under the plan. Each shows a state and opens or does one thing.

| Cell | What it shows | What it does |
| ---- | ------------- | ------------ |
| **Charge now** or **Charging** | Start when not charging, Stop while charging | **Start** starts a charge now and **Stop** stops it now. Start promises no duration: automatic charging may take over again at its next reconciliation. A bare Stop changes the charger and leaves the plan alone. |
| **Schedule active** or **Schedule paused** | Pause or Resume | **Pause** suspends automatic charging and asks for how long: **Until the next planned period**, **Until tomorrow** or **Until I resume**. Only the choices the charger offers are listed. **Resume** clears the pause. |
| **Strategy** | The chosen strategy | Opens **Charging strategy**: **Cheapest**, **Solar** and **Hybrid**, see [charging strategies](strategies.md). A strategy that cannot work on this installation is listed with the reason, for example that Solar requires a site that can measure the solar surplus. |
| **Plan** | Energy, deadline and current, for example `20 kWh · No deadline · 16 A` | Opens the plan settings, see [Plan](#plan-settings). |

Start and Stop are disabled for a user who is not an administrator, and while a start is waiting
for the charger to acknowledge it.

## Status lines

The card words what the integration reports; the integration decides which lines appear and how
serious each is. Examples:

| You see | Meaning |
| ------- | ------- |
| Waiting for tomorrow's prices (~14:00), will plan then. | Prices for the deadline are not published yet; SpotNav plans when they are. |
| Buying 12 kWh now, the rest when the prices are published. | The deadline needs hours that are not priced yet, so part of the charge is bought now. |
| Charging is scheduled from 02:00. / Planned from 02:00. | The installed plan, or a plan that is proposed. |
| A new charging proposal is ready. | A newer proposal exists and is not installed yet. |
| Charging now; scheduled until 05:30. | A charge is running inside a planned period. |
| Automatic charging is paused. / Paused until 07:00. | A pause is in effect. |
| Stopped at 80 % (estimated, reading 40 min old) | The target was reached; shows the level the charge stopped at and how old or estimated it was. |
| Charging is limited to 10 A by the site's load balancing. | Active load balancing has lowered the current. |
| Solar · charging 9 A from surplus | The solar strategy's state: waiting for sun, surplus found and starting soon, surplus fading, charging, or no usable reading. |
| Hybrid · 12 kWh from grid, 8 kWh expected from sun | The hybrid plan's split; with no forecast source it says it plans like Cheapest. |
| Charging was started, but the vehicle is not requesting current. | The connector reports the car is not asking for current. It is an observation only: check the car's charging settings or reconnect the cable. |

![The card while waiting for tomorrow's prices](images/card-waiting.png)

![The card during a charge, with the state of charge estimate](images/card-charging.png)

![The card with the solar strategy on a sunny day](images/card-solar.png)

## The settings popover

The gear icon opens **Card settings**, built fresh each time you open it. The plan and the strategy
have their own cells on the card; everything else is here, in sections.

### Price area and fiscal

On a new charger the area, the phases and the current are already filled in from your Home Assistant
country and location and from the charger, and the card says "Suggested from your location
and charger – check Settings."
Nothing you saved is overwritten. See [first-run defaults](setup.md#first-run-defaults).

Shows the area, the currency and the VAT, energy tax and grid fee as the plan applies them. **Edit
price area and taxes** opens the editor.

![The price area and fiscal settings](images/card-settings-price.png)

- **Area** decides which prices are used and which taxes are suggested. The list is grouped by
  country and is what the public SpotNav Relay publishes; if it cannot be reached, the last list Home Assistant received is
  shown with a note.
- **VAT** (as a percentage), **Energy tax** and **Grid transfer** (both in the area's smallest
  currency unit) can each be switched off, use the area's suggested value, or use your own value.
  **Reset to suggestion** returns to the suggestion when the area publishes one.

### Vehicle

One block per vehicle Home Assistant detected, with the one this charger plans for marked.

![The vehicle settings](images/card-settings-vehicle.png)

- The **vehicle charge level** sensor, and whether it is chosen automatically. **Change vehicle
  charge level** lets you pick another sensor or go back to automatic detection, which matters when
  a vehicle has several battery sensors.
- **Battery capacity** in kWh (1 to 500), unless the vehicle reports it, in which case it is shown
  as reported by the vehicle.
- **Consumption** in kWh per 10 km (0.1 to 50), used to show the distance a plan adds.

Saving each value asks Home Assistant to confirm it; the card never shows a value the integration
did not accept.

### Phases

**Phases the charger uses**: 1 or 3. It sets the power the plan assumes for a given current. A
three-phase charger can still charge a car on one phase.

### Charger entities

For administrators. Lists the charger's entities (**Charge control**, **Current limit**,
**Energy register** and **Vehicle charge level**) and how SpotNav controls the charger: how it
starts and stops a charge, how the current is set, where the charging state is read, the charger's
write limits, and whether load balancing can change the current during a charge. If the charger's
own smart mode is on, the card warns because it can fight SpotNav. **Change charger entities** opens
the editor.

### Site

The section is headed by the site's name and says whether its settings apply to one charger or to
several. A charger with no site says so.

![The site settings with active load balancing](images/card-settings-site.png)

- **Solar priority**: **Car first** or **Battery first**.
- **Solar forecast sources**: the forecast integrations the site may use for Hybrid. Without one,
  Hybrid plans like Cheapest.
- **Active load balancing**: a switch for administrators, best effort and not a protective
  device. The card says when it is not available: the charger is claimed by more than one site, no
  charger on the site accepts a current command, or the site's own power measurement is not healthy.
  Turning it off gives back any current it had lowered, and the card reports each charger it
  restored.
- **Change site entities**: the main fuse, the measurement mode, the per-phase sensors, the signs
  of the meter, the home battery power and the maximum measurement age. **Found in Home
  Assistant** lists the grid meters and home batteries SpotNav recognised, with **Use this** to
  apply one. The editor warns about a source that updates more slowly than the maximum measurement
  age and about devices that balance load by themselves.

## Plan settings

The **Plan** cell opens **Charging plan**.

![The plan settings: energy, deadline, periods and current](images/card-settings-plan.png)

- **Charge by**: **Energy (kWh)** or **Target SoC (%)**. With a target SoC, see
  [target state of charge](target-soc.md).
- **Requested energy**: 0.5 to 100 kWh on the slider, in half-kWh steps; the number field accepts
  more.
- **Finish by a deadline** and **Departure time**. Without a deadline the plan covers the priced
  horizon.
- **Maximum charging periods**: 1 to 8.
- **Planned current**: the current the plan may ask for, in whole amperes, with the nominal power
  it means for the chosen phases. It is a planning value, not a command to the charger.
- With a target SoC: the **Target charge level** slider, the vehicle (when there are several), the
  level now and how old it is or that it is estimated, the vehicle's charge limit when known, and
  the **Energy needed**.

If two clients change the settings at the same moment, the card says they changed elsewhere and
offers **Use the server values** or **Apply my change again**; nothing is overwritten silently.
