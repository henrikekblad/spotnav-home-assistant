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
