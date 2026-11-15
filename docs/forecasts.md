# Forecasts, the calendar and the switched load

What SpotNav 3.0 does today beside charging cars. Each part below says whether it **acts** or is only **shadow**
(computed and shown, never sent to a device). None of it adds a setting except where said.

| Part | Today |
| --- | --- |
| House load forecast | Acts on car plans under an import target (and on the switched load's shadow plan); shown on the site |
| Solar forecast correction | Shadow: logged and scored; the battery plan (also shadow) reads it; the Hybrid strategy still reads the raw forecast |
| Expected solar surplus | A read-only sensor for your own automations; SpotNav acts on nothing from it |
| Calendar departure | Acts: it moves a car's departure and target |
| Switched load | Shadow: SpotNav shows when it would switch it and never does |

## The house load forecast

SpotNav learns what your house draws, hour by hour, and forecasts the next 48 hours.

**What it reads.** The house's measured load per hour over the last six weeks, newer days counting more (a day two weeks ago
counts half as much as yesterday), with weekdays and weekends kept apart. A day that is a public holiday counts as a weekend
day where Home Assistant's Holiday integration provides a calendar. A car's plan counts on a figure with some room (three in four hours the load is below it), not the average:
counting on too little room costs a late car, counting on too much a slightly dearer plan. The battery plan counts on the
typical hour instead, the median, without the in-day adjustment below; the cars use the figure with room and with it. On a site with a home battery or
solar production in the Energy dashboard it uses the house's *whole* load (the grid less the car less the battery, plus what
the sun produced), so the battery's own cycling is not mistaken for the house. With a battery it uses that from the first
hour; with solar only, until it has three days of that it uses the import less the chargers.

It also uses, without any setting:

* the **temperature**: the weather entity's hourly forecast (`weather.forecast_home`, else the only weather entity) and a
  heat pump's own outdoor sensor where one is found. The forecast adjusts for a colder or warmer hour only when the
  temperature really explains your load (an electrically heated house), and has at least a week of hours with a
  temperature; otherwise it leaves temperature out and the card says why.
* **today so far**: once three hours of today are in, a day that runs higher or lower than expected moves the rest of today,
  and fades out over tomorrow.

**How long until it is useful.** It uses what it has from the first day: an hour needs three matching days (the same hour on
three weekdays, or on three weekend days) for its own figure, and until then falls back to the same hour over all days, and
then to a flat figure. So a weekday hour has its own figure after three weekdays and a weekend hour after three weekend days,
which takes two weekends. The first review of how well it did comes the night after its first whole day.

**What it does.** A plan for a car counts on it when an import target is set: the room left for the car is the target less
the house's expected load for each hour, so an evening peak is planned around. On a site where the house is measured as its
whole load (a battery or solar production), the car's room uses a separate forecast of the net import, which already holds
what the battery and the sun really did in each hour. The hold and the main fuse never use it; they
stay on what is measured. It is shown as the site's *House load forecast* sensor (diagnostic) and in the
[house card](house-card.md).

## The solar forecast correction

A solar forecast is often off in the same way every day at one site: shade, a wrong tilt, an optimistic provider. On a site
with a solar forecast chosen, SpotNav learns a factor for **clear** hours and one for **cloudy** hours (60 % cloud cover or
more, from the weather entity) and multiplies the forecast by it.

**What it reads.** The solar forecast the Hybrid strategy uses; the solar production in the Energy dashboard's statistics
and the grid export; the weather entity's cloud cover; and the price area's prices (to see negative-price hours).

**How it learns.** From the last seven days, yesterday counting most, only hours where more than 0.2 kWh was forecast. Once a
night each factor moves a fifth of the way towards what the week showed, and stays between 0.3 and 2.0. A kind of hour
needs three days with such hours before it moves.

**Guard rails.**

* A day with almost no production (under a tenth of the forecast) is skipped: the system was off, not the forecast wrong.
* An hour where production sat flat at the inverter's ceiling is counted as if the forecast was right, so an inverter limit
  is not learned as a weak forecast.
* The corrected forecast never exceeds the larger of the raw forecast and the highest hour ever seen, nor the size of the
  array where the forecast integration states it.
* It never learns from its own output.
* On a site that curtails (an export limit, zero export, or production falling away in negative-price hours) it switches
  itself off and passes the raw forecast through.

**How long until it is useful.** At least three days: the status is *learning* until either clear or cloudy hours have three
days of evidence, and *shadow* from then on. The other kind keeps its factor at 1 until it has three days of its own. Each
factor moves a fifth of the way towards the week's
ratio every night, so it takes some more days to settle.

**What it does.** The corrected and the raw forecast are both logged and scored each night. The battery plan reads the
corrected one once it has learnt. The Hybrid strategy keeps reading the raw forecast. The corrected forecast appears as the
site's *Solar forecast* sensor (diagnostic, `basis` is `raw` until learnt, then `corrected`) and as an option for the Energy
dashboard (see [Home batteries](home-battery.md#forecasts-for-other-energy-tools)).

## The expected solar surplus

The site has an **Expected solar surplus** sensor: how much of the sun the house is not expected to use. It is the sun
forecast (as corrected once the correction has learnt, else as read) less the house's whole-load median, in kWh over the next
four hours. The attributes give the next two hours, the rest of today and one row per hour (`hours`), for blueprints and
automations.

It is unknown while the site has no whole-load figure for the house (a solar or battery site that has not kept enough hours
yet), and a sun forecast older than three hours is not used. It is a prediction: several automations that act on it can
overbook the same sun, so let one of them own it. SpotNav itself acts on nothing from it.

## Departure from a calendar

In a charger's Plan dialog, under the departure, you can choose one of Home Assistant's calendars. SpotNav reads it every 15
minutes, up to seven days ahead.

* **Only the first event of a day counts.** It is that day's departure, in place of the daily one. Later events that day are
  not departures, also once the first has gone.
* **The target is in the event's text.** Write it in the title or description: `mål: 80`, `target: 80 %`, `80%` or
  `30 kWh`. A percentage is used where the charger plans to a level and kWh where it plans an amount; the other kind is
  not used and the dialog says so.
* An all-day event is a departure at the daily time (unless a timed event that day starts earlier). A multi-day event is a
  departure at its start.
* Tick **Only the calendar decides** and a day without an event has no departure. Otherwise such days keep the daily
  departure.
* A date you pick yourself still comes first, unless only the calendar decides (then it is cleared on save). If the calendar cannot be read, the daily departure is used until it can.

More detail is in [Charging strategies](strategies.md) ("Departures from a calendar").

## The switched load (shadow)

In the site's options (**Next: switched load (water heater)**) you can name one switch, its rated power, how long it must run
within any 28 hours and, optionally, a temperature sensor and a lowest temperature. SpotNav works out when it would switch
the load on: in the cheapest hours, within the main fuse and the import target (beside the house's expected load and the
cars' plans), always on when a deadline leaves no slack, and on whatever the price below the lowest temperature. The
fuse comes first.

**In this version it never switches anything.** It writes what it would have done to the site's diagnostics and compares
that with what the switch really did. See [Site and load balancing](site-and-load-balancing.md#a-switched-load-water-heater-in-shadow).
