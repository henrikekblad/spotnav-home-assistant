# The house card (preview)

A second card in the same bundle as the charging card, about the whole house: its load, the sun and the home battery.
It is a **preview in shadow**: it shows what SpotNav forecasts and what its battery plan would do, and it never writes to
anything. The charging card stays about the car.

## Add it

Add a manual card to a dashboard:

```yaml
type: custom:spotnav-house-card
site: <the site's entry id>
```

Use `charger: <a charger's entry id>` instead and the card finds that charger's site (`site` wins if both are given).
The card is also in the card picker as "SpotNav house (preview)". It reads one read-only answer from SpotNav every five
minutes.

## What you see

* **The top**: the battery's charge level, and today's money split the way a battery's own app does: *sold*, *arbitrage*,
  *bought*, with the mode in force ([the battery plan](battery-plan.md)). On a site without a battery there is no battery
  line.
* **The chart** covers 12 hours back and 24 ahead, with two views you can switch between:
  * *Energy*: bars for the house's load per hour (measured before now, forecast after), an area for the sun, and a
    line for the battery's charge level, dashed where it is planned.
  * *Price & battery*: the buying price (every fee included) and the selling price per hour, with the battery's level on
    the right axis.

  Both views tint the hours where the plan charges the battery from the grid (violet) and, more lightly, where it sells to
  the grid.

  Tap an hour for its figures: the house in kW (measured, with the forecast beside it), the sun in kWh (expected for hours
  ahead), the battery's level (planned for hours ahead) and what the plan does in that hour (charges from the grid, sells
  to the grid or holds its charge).
* **The money line**: the last reviewed days, SpotNav's plan against the battery's own way, each measured against plain
  self-consumption. Until a day has been reviewed it says what the plan expects until the end of its horizon, and the
  first comparison arrives the night after.
* **The forecast line**: how far off the forecasts were over the last week: the house in kW, the sun in kWh (with the
  corrected figure beside it where there is one) and the battery's level in points. Before the first whole day has been
  reviewed it says so.

### Details

*Details* opens, as far as the site has them:

* **Energy today so far**: where today's energy came from (sun, battery, grid) and where it went (battery, use, grid). The
  sun counts only the hours Home Assistant has already compiled.
* **Saved per day**: the month's reviewed days, the plan beside the battery's own way, and the month's arbitrage.
* **Battery modes side by side**: for Self-consumption, Balanced and Max profit, what each is planned to save ahead and what
  it would have saved over the reviewed days, with the mode in force marked, and the battery's own way (or its own optimiser)
  for comparison. All are against plain self-consumption on the same measured load and sun.
* **Day by day**: each reviewed day's mean forecast error and saving.
* **Sources**: the weather entity, outdoor temperature, holiday calendar and solar forecast the forecasts read, whether
  temperature is in the house forecast (and why not), the battery, and what is stored in it now with its average cost
  (marked estimated until the first measured energy has been used). A note lists what the plan assumed, such as the
  battery's lowest level.

The card says plainly when the battery runs its own optimiser ("SpotNav plans beside it only to compare, and writes nothing
to it") and when it is in shadow ("SpotNav plans the battery but writes nothing to it").

Money is in the price area's minor unit (öre, cent) as the plan counts it. Nothing here controls a charger or a battery. The
forecasts behind the card are described in [Forecasts](forecasts.md).
