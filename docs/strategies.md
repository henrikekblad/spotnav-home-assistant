# Charging strategies

A charger has one strategy at a time, chosen in the card (**Strategy**). A strategy that cannot
work on this installation is listed as unavailable with the reason.

## What every strategy shares

The plan is calculated from the charger's settings: price area, current in amps, the
energy to charge (or a [target state of charge](target-soc.md)), the maximum number of charging
periods, and an optional departure time. The card marks what is missing.

- **Automatic execution.** Home Assistant installs the plan and starts and stops the charger at
  the planned times. It survives restarts. **Start** and **Stop** always override the plan
  for the moment, and **Pause** suspends automatic execution until the next period, until
  tomorrow, or until you resume it.
- **Settings are one record.** The card, the app and the entities all edit the same settings.
  A write names the revision it edited, so two clients cannot silently overwrite each other.
- **Prices** come from the SpotNav Relay, exactly as published: EUR per kWh with each area's
  currency, units and time zone. Home Assistant never converts or guesses. Fiscal components
  (VAT, energy tax, grid transfer fee) can each be off, use the relay's suggestion for the
  area, or use your own value.
- **Tomorrow's prices** are published in the afternoon. If a deadline needs hours that are not
  priced yet, SpotNav buys what it must now and plans the rest when the prices arrive. Part of
  a plan that had to be bought without published prices is flagged.

## Cheapest

Buys the required energy in the cheapest quarter-hours before the departure time, within the
maximum number of periods. With no departure time the plan covers the priced horizon.

**Equal prices charge late.** When several quarter-hours cost exactly the same (a flat price, or a
fixed price that does not change through the day), SpotNav picks the latest ones before the
departure. The car stands plugged in, starts as late as the prices allow and leaves with the freshest
charge. Earlier versions picked the earliest of equal quarter-hours, so a flat day started the charge
at once.

**A departure on a particular day.** In the card's Plan dialog, next to the departure time, you can
choose a date up to seven days ahead ("Sun 4 Oct"); clearing it returns to a departure that repeats
every day. With a date the plan reaches that far, but SpotNav still only charges in prices that are
published. If the weeks behind show that the same weekday and hours that are not published yet were
clearly cheaper (more than one standard deviation of the spread, from a history the relay publishes),
it waits and says so: *Waiting: Sundays were 30 % cheaper the last 4 weeks*. It only waits while the
charge can still be finished in time, buys what cannot wait, and plans again each time prices are
published. Without a usable history, or without a clear saving, it plans on the published prices.
A date that has gone by is ignored and forgotten the next time the settings are saved.

**Only some weekdays.** In the card's Plan dialog, under **Every day**, the weekday buttons choose
the days a daily departure applies on (all seven by default). On a day that is not chosen there is no
departure and the plan runs to the next chosen day, with the same rules as a departure on a particular
day: it charges in published prices and may wait for cheaper unpublished hours. A date you pick
yourself overrides the weekdays.

**Plugging in and unplugging.** SpotNav plans again a few seconds after the car is plugged in or
unplugged, and again the moment the departure passes, without waiting for the next price. A car
plugged in while a planned period is running starts charging at once, unless you stopped the charge
yourself during that period, Auto is paused, solar charging owns the charger or load balancing has no
room for the minimum current. A charge the charger starts by itself inside a planned period counts as the plan's, so it stops with the
period or when the need is met. Plugged in between periods, it waits for the next one as before. Only a
status that shows a car there or gone counts as a plug-in or an unplug: a fault, an offline or updating
charger, OCPP's Unavailable and a Wallbox's Ready (shown with or without a car) say nothing either way. When
the need is met before the plan has run out (the target is reached, or the requested energy has been
delivered, also by **Charge now** or by the sun) the periods still ahead are cleared and the charge a
period started is stopped; a charge you started yourself goes on.

**Counting the requested energy.** With a departure, energy delivered since that departure's
previous occurrence counts toward the request, also across unplugging and plugging in again. With no
departure, each plug-in starts a new count; a charger that cannot report a plug-in starts one with the
first charge after the request was met that neither you (**Charge now**) nor the sun started. The count comes from the charger's energy register; a register
that starts again from zero at each plug-in is counted on. A reading that drops for a moment (a charger
that restarts) or rises by more than the charger can have delivered while it was charging is not
believed, so it never ends a plan; a count that a later reading proves false opens the need again
once it stands at least 0.3 kWh below the request for two minutes, so a register wavering at the
request does not stop and start the charge. If the register cannot be read, the last
remaining energy it showed is kept rather than buying the whole request again, and with no register
at all the charger's recorded charges since the count began are used; the status says so either way.

**No more than the battery has room for.** When the car's level (a reading or an estimate) and its battery
size are known, a requested amount is capped at the room left: battery size x (the car's own charge
limit, else 100 %, minus the level) / 100, divided by the charging efficiency. A car at 96 % with 77.4 kWh
is planned 3.4 kWh, not the 43.5 kWh asked for, and the status says *Limited to 3.4 kWh: the car is almost
full*. Such a charge, like a target at or above the car's own limit (or 100 %), is ended by the car: SpotNav
keeps it on within the planned periods and never stops it on an estimate or the delivered energy; a car
that then stops taking current is full, not a fault. The card's and the app's kWh slider goes no higher
than that room.

**The car finishes, even past the last period.** A car near its own limit lowers the current over the
last half hour or so, so the plan's last period can end while it still draws. For a charge the car ends
itself, SpotNav then keeps the charge on until the car stops drawing by itself (no current, or the car
saying it wants none, for two minutes in a row), at most an hour past the period's end and never past
the departure. The status says *Charging until the car is full (at most until 06:40)* meanwhile, and a
car that stops is told as *Charging complete: the car is full*; reaching the hour ends the charge as the
period's end would have, with no warning that it stopped. A Stop, a pause or the car being unplugged ends
it at once, load balancing still limits and pauses it, and a car that has already stopped drawing when
the period ends is not kept on. A target below the car's limit, or an amount the battery has room for,
still ends with its last period.

A daily departure whose morning lies beyond the last published price (before the afternoon
publication) waits for the publication when that is safe. The history only explains that wait
(*Waiting: Saturdays were 30 % cheaper the last 4 weeks*), or ends it when the hours still available
are clearly cheaper than the expected unpublished ones. Without a clear signal nothing changes.

## Solar

Charges only from surplus solar power, modulating the charger's current with the surplus.
Requires a [site](site-and-load-balancing.md) whose measurements let SpotNav compute the
surplus: derived phase measurement from power and voltage, or a direct (current per phase) site
that also has the meter's **total grid power** (see
[Total grid power](site-and-load-balancing.md#total-grid-power-for-solar)), and optionally the
charger's own measured current and a house battery power sensor. The card says when a site
cannot, and for a direct site without the total it says the meter's total grid power is needed.

- The surplus is computed from the site's energy balance (grid, house battery and the car's
  own draw), never from export alone, so that the car's draw does not count as surplus.
- Charging starts after the surplus has held for a while and stops after it has faded, to
  avoid rapid switching. With no usable measurement it stops rather than guesses.
- A charge starts at the minimum current and stays there for two minutes while the car's own draw
  shows the surplus is real; only then does the current follow the surplus. Under *car first* a
  start that counted a charging house battery is stopped at once if the battery then turns to feed
  the car, and a charging battery is not counted again for ten minutes (twice as long after each
  such start, up to four hours). A discharging battery never counts as surplus.
- A charge the charger begins by itself (at plug-in, say) is taken over: its current follows the
  surplus, down to the minimum at once when there is too little, and it stops when the surplus does
  not come back. A charge you start with **Start** is yours and is left alone.
- After you press **Stop**, the sun does not start the charge again until the car is plugged in
  again, you press **Start** or a planned period begins; the status says so.
- **Solar priority** (a site setting): *car first* uses surplus before the house battery;
  *battery first* leaves the surplus to the battery and charges the car from what it does not
  take. A house battery power sensor can be set on the site.
- Several chargers on one site share the surplus by their
  [charger priority](site-and-load-balancing.md#charger-priority): the first in the order is
  offered it all, the next only what the first cannot use.

## Hybrid

Cheapest planning that may hold back grid energy in the hope of sun. Choose one or more solar
forecast sources on the site (any integration that feeds the Energy dashboard's solar forecast,
for example Forecast.Solar or Solcast). Without a forecast source, hybrid plans exactly like
cheapest.

- It never holds back more energy than it could still buy before the deadline if the sun failed
  completely, so hoping for sun can make charging dearer but never late.
- It holds back energy only when the expected solar saving is worth the price risk.
- Every replan re-reads the remaining need, so a cloudy day moves energy back to the grid.
- When the expected sun covers the whole remaining need, nothing is bought from the grid: the status
  reads *Hybrid · 0 kWh from grid, … kWh expected from sun*, and the next replan buys again if the
  forecast drops.
- Outside a planned period the sun's rules above own the charger, also when there is no plan at all
  because the sun covers the need: a charge the charger began by itself is taken over and follows the
  surplus, and a Stop sticks as under Solar.
