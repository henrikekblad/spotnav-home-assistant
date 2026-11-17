# The house card (preview)

A second card in the same bundle as the charging card, about the whole house: its load, the sun and the home battery.
Its header is the charging card's: the SpotNav mark, "House" and a muted status after it. Until battery control is on
and checked, the status says "· Test" (tap it: "SpotNav is not controlling anything yet" and what that means): the
card shows what SpotNav forecasts and what its battery plan would do, and writes to nothing. While the battery's own
optimiser runs it (Sigenergy's AI mode, for one) it says "· Own control". Once SpotNav controls the battery
([Home battery](home-battery.md)), it says what the battery does now: "· Charges from the grid", "· Holding",
"· Sells to the grid" or "· Discharging" for the action SpotNav runs, else, as the battery's own self-consumption
measures, "· Charging from the sun", "· Discharging" or "· Idle". The mode is in the Now tab, under its picture.
The card itself never writes to anything. The charging card stays about the car; the house card is where a home without a charger starts.

## Add it

Add a manual card to a dashboard:

```yaml
type: custom:spotnav-house-card
site: <the site's entry id>
```

Use `charger: <a charger's entry id>` instead and the card finds that charger's site (`site` wins if both are given).
The card is also in the card picker as "SpotNav house (preview)", set to the first charger's site, else the first site.
It reads one read-only answer from SpotNav every five minutes, and, while its Now tab is shown, the live flows every 30
seconds.

A wall screen can show one tab only, without the tab row: `tab: now`, `flow`, `graph` or `price` (the card editor's
**Show only tab**; `all`, or leaving it out, shows the row). It is a setting of this card, not of SpotNav. In a box of
fixed height (a layout card on a wall screen, a sections view with a number of rows) one part of the shown tab takes the
box's spare height (the picture in Now, the diagram, the chart, the price chart) and the hints and footers go: the tiles
under the energy flow, the tap hints. Every tab fits a 650×520 box with the tab row shown.

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
  water heater on a relay. Otherwise the [heat settings](heat-pumps.md) in full: on the relay path the **Styr värmen**
  switch with its line ([SpotNav's own heat control](heat-pumps.md#spotnavs-own-heat-control-relay-off-by-default)),
  the sensors with each indoor sensor's offset, the control path, hot water, the zones and the cooler night.
* **Support**: download the debug info.

## What you see

Above the tabs only the header. Then four tabs, in this order: **Now**, **Energy flow**, **Energy chart** and **Price
and battery**. Tap one, or move with the arrow keys (Home and End for the first and last; the tabs are a tablist for
screen readers). A long tab name wraps between its words, so all four stay in view at 360 px. The card remembers the tab
per card in this browser.

### Now

The live flows as a picture: the house in the middle and around it only what the site has: the sun (top left), the grid
(top right), the battery (bottom left), the car (bottom right) and the heat under the house. A line runs from each to the
house, with dots running the way the power flows, a filled arrowhead on its middle and its kW beside it. The dots are
amber for the sun, teal for the battery, the car's and the heat's own colours, red for an import and green for an export;
they run faster with more power, and stand still where the browser asks for reduced motion. The house's own power (the
heat included, the car not) is in its circle. The car's line is drawn only while a car is plugged in; otherwise its node
is dimmed. A house with the grid alone is one low row, the grid to the house.

In the corners, the day so far, each figure in its part's colour (a little darker on a light card, lighter on a dark
one): *Sun today*, *House today* (the heat included), *Bought today* with what was sold in green, the battery's level and
the energy in it, *Heat today*, and the car's level with "25.5 kWh today" under it (its energy today alone while no car
is plugged in). These are the energy flow's own figures, so they add up with the Energy flow tab. A name too long for
its corner ends in "…".

Under the picture: one sentence of what the battery does and what its plan does next ("The battery covers the house and
stores the sun until 18:00, then it sells to the grid."); while SpotNav does not control the battery it says who does and
what SpotNav would do ("The battery runs itself (Sigenergy AI) · SpotNav would …"). Then the day's money: "Today: the
battery earned 11 kr · SpotNav saved 17 kr". The battery's figure is its *arbitrage*: each kWh it gave back, over what
that energy cost to store ([the battery plan](battery-plan.md)), the same measure the battery's own app shows. SpotNav's
is against a comparison of its own per part (below). Small and grey under it: the mode, and a learning period that holds
something back ("Learning the house's peaks, 13 days left. Does not charge from the grid until then."). A heat pump meter
to choose (more than one could be it) is said here.

The live figures are read every 30 seconds while Now is shown: the grid and the chargers as load balancing reads them,
whether a car is plugged in and its level, the battery's own power, the sun from the Energy dashboard's solar power
sensors and the heat from the heat pump's meter where it is a power sensor (else the last quarter hour's mean). They are
written into the picture in place: the dots keep running and only start again when a line turns. From an older SpotNav
the picture has the day's figures and the battery's own power only, and says "no live figures yet".

### The period tabs

Energy flow, Energy chart and Price and battery share one row to choose the period: "‹ Today ›" and **Day | Month |
Year**. The arrows step one period; tap the label ("Today", "Yesterday", "Wed 11 Nov", "Nov 2026", "2026") for the
calendar or the list. Today's label in Energy flow says how far its figures reach ("Today · to 17:00"). Nothing after
today is offered, and nothing more than ten years back. The period stays chosen as you switch between the three.

* **Energy flow**: where the period's energy came from and where it went (the diagram below in *Heat in the flow* and
  *How the flow is counted*), and a tile each for *Sun*, *House*, *Discharged*, *Charged*, *Bought* and *Sold* in kWh,
  plus *Heat* and *Car* where the site has them: the diagram's own blocks. Tap House (or Heat) for what used it.
* **Energy chart**: today covers 12 hours back and 24 ahead: bars for the house's load per hour (measured before now,
  forecast after in a lighter shade), the heat in it, the car where it is shown, and an area for the sun; over a past bar
  a short line shows what the forecast was. A past day has its 24 hours. A month has a bar per day and a year a bar per
  month, as wide as the chart allows, with the house, the heat and the car stacked and the sun as a stepped line over
  them, on a kWh axis. Tap a bar or an hour for its figures; the arrow keys walk them.
* **Price and battery**: headed by what the battery earned in the period ("The battery earned 11 kr", its arbitrage, the
  sum of the bars), with "The same measure as the Sigenergy app" and what the electricity cost under it. A day has the
  buying price (every fee included) and the selling price per hour, with the battery's level on its own 0–100 % axis,
  dashed where it is planned; the hours the plan charges from the grid are tinted violet, those it sells more lightly.
  Under it, on the same hours and the same time axis, what the battery earned each hour as kr bars (below the line where
  it gave energy back for less than it cost to store); a tap on either is that hour. A month has a kr bar per day and a
  year one per month. The battery's hourly figures are kept 40 days, its daily ones 400.

### The history

The clock in the header opens the history: a day, month or year with its bars of what the house cost, the share of the
perfect plan, the energy flow, the money and what SpotNav saved. With *Detailed* on it adds the modes side by side, the
forecasts' errors, the days, and **the last days as the card reviews them** (below), led by the reviewed week's sentence
("Last 7 days: SpotNav's plan 51 kr · the battery itself 45 kr, against plain self-consumption").

**The comparison** of the modes, titled with what it compares against ("Compared with the battery only storing your own
sun, last 7 days"): each of SpotNav's modes (the chosen one marked) and the battery's own control, named where the
battery runs its maker's optimiser ("Sigenergy AI"), over the last reviewed days. It is not the battery app's measure: a
battery's own control can earn money in the app's terms and still come out below plain self-consumption here. Once a
reviewed day has its **perfect plan** it is each way's share of it instead. The perfect plan is the best the battery could
have done that day in hindsight, with the measured house, sun and car and the same battery, against the same day with no
battery. The rules that keep it fair:
  * Every way is measured against one perfect plan, under the most permissive rules any of them used, so none can
    beat it: where selling was possible that day (the site sent energy to the grid, or the battery's own control or a
    SpotNav mode sold) it is allowed to sell, with Max profit's rules; otherwise it does not sell either. The note
    under the shares says which.
  * Every way starts the day at the same measured level, and what is left in the battery at midnight counts at the
    next day's mean buy price (with fees) less the battery's losses: a price that is known at the time, as the next
    day's prices come at about 13:00. Until they are in, the day uses its own price and says so.
  * Only whole days count: a day with under 90 % of its quarter hours measured is an "incomplete day", with no share.
  * A share is shown only where the perfect plan saves at least 5 kr a day (5 € on a euro site); below that it says
    "too small a difference to compare". A week or a month sums the savings over its whole days first and then
    divides, so a charge on one day and the discharge it pays for on the next even out.

### Heat in the flow

**Heat** is the heat pump's meter and the heating zones' own meters together, each counted once; **House** is what is
left, so the totals still add up. A heating zone is a thermostat chosen under Settings → Heating → Zones: electric
floor heating or an air-to-air heat pump. There is no setting for its meter: SpotNav reads an energy sensor on the
thermostat's own device in Home Assistant, else its power sensor. A zone with neither is left out of Heat (the
diagnostics, `heat.meter.zones`, list it and why). Nothing is counted twice: a zone whose sensor is the heat pump's
meter, sits on the heat pump meter's device, or is nested inside the heat pump's meter or another zone on the Energy
dashboard, is left out, and so is a second thermostat on a device already counted.

Where Heat holds more than one source, tap **Heat** (or focus it and press Enter) for its own pie: the heat pump and each
zone by name, with kWh and %. The same pie opens from the history's flow for a day, month or year.

The hour chart's heat (and its forecast) and *Heat today* hold the zones too, from the hour they were added. A zone
added today makes a step in the heat forecast's review, which settles within a week.

### What the house used

Tap **House** in the energy flow (or focus it and press Enter) for a pie of the house's energy over the same day, or in
the history the day, month or year shown:

* each meter of Home Assistant's Energy dashboard under **Individual devices**, by its name there (at most eight
  named slices). A meter under 1.5 % of the house, or past the eighth, goes into a light grey **Other metered** slice
  and is named under the pie. A darker grey **Unknown** slice is what no meter explains: the house less all the meters.
  There is no setting: SpotNav reads the meters the Energy dashboard already has.
* The car and the heat are not in it: the flow shows them on their own. SpotNav sorts these meters out by itself and
  counts each only there: a meter whose sensor is a charger's, the heat pump meter's or a heating zone's, or that sits on
  the same Home Assistant device as a charger or a heating zone. (The heat pump's meter is told by its sensor alone: a
  clamp meter with several channels has the heat pump on one and other circuits, such as the stove, on the others.) A
  device nested in another one counts once, in its parent.
* Where the meters add up to more than the house (a device meter that also measures the car or the heat, or meters that
  disagree), Unknown is shown as 0 and a line says by how much.
* An hour a meter cannot have used (more than the whole house used that hour, or more than the main fuse allows) is
  not counted: it is a broken statistic, typically a plug whose total drops and comes back, which Home Assistant counts
  as new energy each time. The meter is shown with its other hours, and one line names it and how many hours were left
  out, with where to repair it: Developer tools → Statistics. A meter with no believable hour is left out of the pie
  and the line says so. The same holds for the heat pie's sources.
* Where the meters add up to more than the house (a device meter that also measures the car or the heat, or meters that
  disagree), Unknown is shown as 0 and a line says by how much.
* With no individual devices the pie is Unknown alone, with a line on where to add them: Settings → Dashboards → Energy →
  Individual devices.

The figures come from the recorder's statistics, by the hour and for today's running hour by five minutes, and are read
only when the pie is opened.

#### Where a meter belongs

Some meters SpotNav cannot tie to a load: a Shelly in the fuse box on the floor heating's circuit, a plug in front of
the car's charger, or a meter that sits inside another one. An administrator taps its slice (or its name, with the
keyboard) to choose **Belongs to**: **House** (the default), **Heat**, **Car** or **Don't count**.

* **Heat**: the meter is counted in Heat (the flow's block, the hour chart and the heat pie, marked "changed by you")
  and taken out of the house pie, so House drops by as much.
* **Car**: the meter is taken out of the house pie as already counted with the car. The car's figure stays the
  chargers' own, so nothing is counted twice.
* **Don't count**: the meter is counted nowhere, for a meter already inside another one.

A meter moved out of the house is listed under the house pie with where it went and a button to bring it back. The
choice is saved per meter in the site's settings (`meter_belongs`); a household member sees the pies without the
choice. Only meters SpotNav did not sort out by itself can be moved.

### What SpotNav saved

Each part is compared with what would have happened without SpotNav, and only what SpotNav itself did counts.

* **Battery**: the battery's earnings of the day (the headline's measure), in the share of the day SpotNav controlled
  the battery. In test ("· Test" in the header), with SpotNav's control off, or while the battery runs its own
  optimiser, the battery is not part of SpotNav's saving at all: what it earned is its own, and the headline says
  "(own control)".
* **Car**: against charging at once on plug-in. The same kWh are charged from the moment the car was plugged in, at the
  power the car actually drew (the highest rate of the plug-in), until they are in or the car was unplugged, at the buy
  price of those quarter hours with every fee, as the card prices hours. The saving is what that would have cost less
  what the charge cost. A charge someone started (or one SpotNav cannot tell the start of) saves 0 but is still the
  car's; a charge that started at plug-in anyway saves about 0. The car is counted from the first charge SpotNav kept
  the plug-in time of, so the line has no car part before that. A charge is counted on the day it **started**, the same
  day the charger card's history counts it, so a charge over midnight is one figure on one day in both cards. The
  charger card's history shows the same figure per charge, day and month ("SpotNav saved 33 kr against charging at once
  on plug-in").
* **Heat** (estimated): the heat control's own review of the day: what SpotNav's lowerings of the relay took away at
  their hours' prices, less the learned rebound bought back afterwards ("Yesterday the relay lowered 3 h: 1.58 kr"), in
  the share of the relay's closed time SpotNav itself held it. What SpotNav's plan would have done in test never counts.
  A day is reviewed the night after, so today has no heat part yet.

**Nothing is counted twice.** Only the car's kWh from the grid count in its saving. The flow's rule says, hour by hour,
how much of the car's energy came from the grid, the home battery and the sun. The kWh the battery gave the car are
already in the battery's earnings (it values every kWh it gives the house, the car included, at the buy price), and the
sun's are worth the selling price whenever they are used, so both are left out of the car's saving, in what happened and
in the comparison alike. The battery's figure holds no part of the car's. An hour the flow has no figure for counts all
the car's kWh as grid on a site with neither battery nor sun, and none of them on a site with either.

The history (the clock in the card's header) has the same parts summed per day, month and year: a *SpotNav saved* box
with the total and a row per part (with the days it covers where fewer than the period's), and each day's or month's bar
adds its total to its figures. The battery's day money and the heat's reviews are kept about a month, so an older day
has only the car.

### The last days, in the history

The history's *Detailed* has, as far as the site has them:

* **What SpotNav saved**: one line per part saying what it is compared with, that the heat is an estimate, a battery
  under its own control, and from which day the car is counted.

* **How the flow is counted**: the meters do not say which source fed which use, so the split follows a fixed rule, not a
  measurement: each hour (or quarter hour) the sun goes to the house first, then the car, the battery and the grid, and
  the battery covers the house before the grid does. Where the car or the heat pump's meter could not be read every hour,
  it says so. (Without the diagram, today's energy is shown here as two bars instead; the sun counts only the hours
  Home Assistant has already compiled.)
* **The battery app's measure**: the same day in the terms the battery's own app uses, which are not the comparison's.
  Two baselines, no setting:
  * *Battery against no battery*: what the day really cost (bought at the buy price, less sold at the sell price) against
    the same day with no battery at all: the house and the car on the grid, the sun still there. A positive figure is
    money the battery saved. This is the one to hold up against the battery app's own figure. The energy the battery holds
    at the end of the day is not counted in it, which the comparison does count: a battery that emptied itself looks
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
