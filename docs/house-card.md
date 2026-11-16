# The house card (preview)

A second card in the same bundle as the charging card, about the whole house: its load, the sun and the home battery.
Until battery control is on and checked, the chip in its header says "Test · not controlling yet" (tap it for two
sentences on what that means): the card shows what SpotNav forecasts and what its battery plan would do, and writes to
nothing. Once SpotNav controls the battery ([Home battery](home-battery.md)), the chip says "Controls the battery".
The card itself never writes to anything. The charging card stays about the car; the house card is where a home without a charger starts.

## Add it

Add a manual card to a dashboard:

```yaml
type: custom:spotnav-house-card
site: <the site's entry id>
```

Use `charger: <a charger's entry id>` instead and the card finds that charger's site (`site` wins if both are given).
The card is also in the card picker as "SpotNav house (preview)", set to the first charger's site, else the first site.
It reads one read-only answer from SpotNav every five minutes.

## Settings

An administrator sees a gear in the card's header. It opens the **settings popover**, the same one the heat card's gear
and the charging card's **Settings** share, for the card's site. Its sections follow what the site has:

* **Get started**: what is left for the site: add a charger (answer **Not needed** for a home without one), an
  electricity price, the grid meter's sign, a solar forecast, the home battery, heating.
* **Electricity price**: the same rows as the charging card's: which electricity price the site uses (and **Price
  from** where there are several), the price area, VAT, energy tax and grid fee with the grid tariff beside it, the
  supplier's markup and what a sold kWh earns (shown where something is sold), and the demand charge. Without an
  electricity price, one row that adds one.
* **The site**: main fuse, import target and the meters.
* **Solar**: the solar forecast sources, as on the charging card.
* **Home battery**, when SpotNav finds one: the battery control switch, what is missing, the check and allowing sales
  ([home batteries](home-battery.md#spotnavs-own-battery-control-off-by-default-in-testing)).
* **Heating**, always: one line "Nothing found" until a heat pump, a heat meter or a thermostat is found; tap it to add a
  water heater on a relay. Otherwise the [heat settings](heat-pumps.md) in full.
* **Support**: download the debug info.

## What you see

* **The top**: the battery's charge level with the chosen mode, then today's headline: what the battery has earned so far
  ("The battery has earned 26 kr today"): each kWh it gave back, over what that energy cost to store (the *arbitrage* of
  [the battery plan](battery-plan.md)). That is the same measure the battery's own app shows, and the card says so right
  under it. Below that, what the electricity bought cost and, when there is some, the sun sold. On a site without a battery
  there is no battery line.
* **The chart** covers 12 hours back and 24 ahead, with two views you can switch between:
  * *Energy*: bars for the house's load per hour (measured before now, forecast after in a lighter shade) and an area
    for the sun. Over a past bar a short line shows what the forecast was for that hour.
  * *Price & battery*: the buying price (every fee included) and the selling price per hour, with the battery's level on
    its own 0–100 % axis on the right, dashed where it is planned.

  The price view tints the hours where the plan charges the battery from the grid (violet) and, more lightly, where it sells to
  the grid.

  Tap an hour for its figures: the house in kW (measured, with the forecast beside it), the sun in kWh (expected for hours
  ahead), the battery's level (planned for hours ahead) and what the plan does in that hour (charges from the grid, sells
  to the grid or holds its charge).
* **Past days**: browse earlier days with *Previous day*, *Next day* and *Choose a day*; a day
  nothing was measured on says so.
* **The comparison**, titled with what it compares against ("Compared with the battery only storing your own sun, last 7
  days"): each of SpotNav's modes (the chosen one marked) and the battery's own control, named where the battery runs its
  maker's optimiser ("Sigenergy AI"), over the last reviewed days. It is not the battery app's measure: a battery's own
  control can earn money in the app's terms and still come out below plain self-consumption here. Until a day has been
  reviewed a line says what the plan expects until the end of its horizon, and the first comparison arrives the night
  after.
* **Energy today so far**: a diagram of where today's energy came from and where it went (see Details for how it is
  counted), and the heat pump's share of the house where its meter is known.

### Details

*Show details* opens, as far as the site has them:

* **How the flow is counted**: the meters do not say which source fed which use, so the split follows a fixed rule, not a
  measurement: each hour (or quarter hour) the sun goes to the house first, then the car, the battery and the grid, and
  the battery covers the house before the grid does. Where the car or the heat pump's meter could not be read every hour,
  it says so. (Without the diagram, *Energy today so far* is shown here as two bars instead; the sun counts only the hours
  Home Assistant has already compiled.)
* **The battery app's measure**: the same day in the terms the battery's own app uses, which are not the comparison's.
  Two baselines, no setting:
  * *Battery against no battery*: what the day really cost (bought at the buy price, less sold at the sell price) against
    the same day with no battery at all: the house and the car on the grid, the sun still there. A positive figure is
    money the battery saved. This is the one to hold up against the battery app's own figure. The energy the battery holds
    at the end of the day is not counted in it, which the comparison on the front does count: a battery that emptied itself looks
    better here than there.
  * *What the sun gave*: the day with the sun and no battery against the same day with no sun and no battery (the sun used
    in the house at the buy price, the sold sun at the sell price). The sun's production comes from the Energy dashboard's
    solar statistics, read a little after midnight. *Sun and battery together* is then against a day with neither.

  All of it is worked out from the same measured quarter hours as the comparison, so a day the meters missed part of says
  how many of its quarter hours the figures cover, and how many of them had the sun's production. Today shows the sums of
  the reviewed days.
* **The forecast line**: how far off the forecasts were over the last week: the house in kW, the sun in kWh (with the
  corrected figure beside it where there is one) and the battery's level in points. Before the first whole day has been
  reviewed it says so.
* **Saved per day**: the month's reviewed days, the plan beside the battery's own way, and the month's arbitrage.
* **Battery modes side by side**: for Self-consumption, Balanced and Max profit, what each is planned to save ahead and what
  it would have saved over the reviewed days, with the chosen mode marked, and the battery's own way (or its own optimiser)
  for comparison. All are against plain self-consumption on the same measured load and sun.
* **Day by day**: each reviewed day's mean forecast error and saving.
* **Sources**: the weather entity, outdoor temperature, holiday calendar and solar forecast the forecasts read, whether
  temperature is in the house forecast (and why not), the battery, and what is stored in it now with its average cost
  (marked estimated until the first measured energy has been used). A note lists what the plan assumed, such as the
  battery's lowest level. Where the house's load is measured as the grid less the chargers, a note says that it looks
  smaller while the battery covers the house.

The card says plainly when the battery runs its own optimiser ("SpotNav plans beside it only to compare, and writes nothing
to it") and when it is in test ("Test: SpotNav plans the battery but does not control it yet").

Money is in the price area's minor unit (öre, cent) as the plan counts it. Nothing here controls a charger or a battery. The
forecasts behind the card are described in [Forecasts](forecasts.md).
