# Prices

Where SpotNav's prices come from, what is added to them, and how the integration uses them. The
last part describes the day document SpotNav keeps and hands to the app, for anyone who reads it
directly.

- [Where prices come from](#where-prices-come-from)
- [What you pay](#what-you-pay)
- [How the integration uses the prices](#how-the-integration-uses-the-prices)
- [The day document](#the-day-document)

## Where prices come from

Your Home Assistant fetches the prices itself, for its own use, from a source you add once. Which
source an area uses follows from what you have set up; there is no setting to choose it:

| Area | Source | What you add |
|---|---|---|
| Sweden, Norway, Denmark, Finland, Estonia, Latvia, Lithuania, Germany/Luxembourg, the Netherlands, Belgium, France, Austria, Poland, Romania, Bulgaria | Nord Pool, through Home Assistant's built-in [Nord Pool](https://www.home-assistant.io/integrations/nordpool/) integration | The Nord Pool integration (**Settings → Devices & services → Add integration → Nord Pool**) |
| Spain – PVPC (regulated) | Red Eléctrica, through Home Assistant's built-in [PVPC](https://www.home-assistant.io/integrations/pvpc_hourly_pricing/) integration | The PVPC integration |
| Great Britain – Octopus Agile (14 regions) | Your own Agile rates, through the [Octopus Energy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy) integration | The Octopus Energy integration, from HACS, with your Agile meter |
| Every other area: Switzerland, Czechia, Slovakia, Hungary, Slovenia, Croatia, Greece, Italy (all zones), Spain (day-ahead) and Portugal | The ENTSO-E Transparency Platform | Your own free ENTSO-E key |

- **Nord Pool.** SpotNav asks the integration's `nordpool.get_prices_for_date` for each day of the area it
  needs, in euro. The area and currency you pick when adding the integration do not matter. Nord Pool's
  day is the CET day, so a Finnish, Baltic, Romanian or Bulgarian day is cut from two of Nord Pool's days.
  Nord Pool keeps about two months of past days.
- **Your ENTSO-E key** works for every area the platform publishes, the Nord Pool areas included; where
  both are there, Nord Pool is used. To get one: register at
  [transparency.entsoe.eu](https://transparency.entsoe.eu/), then e-mail transparency@entsoe.eu with the
  subject "Restful API access" and your account's e-mail address in the text. Once access is granted (it can
  take a few days), generate the security token under **My Account Settings**. Enter it in the card under
  **Settings → Electricity price**, or in the electricity price's **Configure**. There is one key for the
  whole installation. It is stored in Home Assistant, never logged and never shown in diagnostics; ENTSO-E
  checks it once when you save it.
- **PVPC** reads the prices the PVPC integration already holds. **Octopus Agile** reads your meter's rates
  from the Octopus Energy integration, so they are your own tariff's prices; see
  [Great Britain](supported.md#great-britain-octopus-agile).

An area with no source of its own shows a notice in the card (**Settings → Electricity price**) with the
one step that gives it one ("Add Nord Pool", "Add your ENTSO-E key", "Add PVPC", "Add Octopus Energy"),
and the electricity price device names it as its problem. Prices it already holds keep showing. Once
there is nothing priced for today, Home Assistant also raises a Repairs issue; it goes away by itself
when prices arrive. See [Troubleshooting](troubleshooting.md#prices-are-missing).

The SpotNav relay no longer delivers prices (since SpotNav 2.0); it only wakes the app for its
notifications.

**What SpotNav contacts for prices.** Only Home Assistant's own integrations that you added (which
contact Nord Pool, Red Eléctrica or Octopus Energy themselves), the ENTSO-E platform
(`web-api.tp.entsoe.eu`) when you have entered a key, and the European Central Bank's public reference
rates (`www.ecb.europa.eu`, once a day) for an area priced in another currency than the euro. Nothing
about your charging is sent anywhere.

You choose the price area in the card (**Settings, Electricity price**; see [the card](card.md#electricity-price)). It belongs to
the installation's [electricity price](price-device.md), so every charger that uses it follows. A country with several
bidding zones (Sweden, Norway, Denmark, Italy) has one area per zone. The card names each area's
price source under the area's prices.

### Currency

Every day is stored in euro per kWh, together with the ECB reference rate for the area's currency
and the date of that rate (the newest rate at most a week before the day). Home Assistant converts with
that rate, the one in the day's own file, and shows the price in the area's currency: kronor and öre in Sweden, pounds and pence in Great
Britain, euro and cent in the euro areas. If a day's file has no rate for a non-euro area, SpotNav
does not guess one: the card says the prices cannot be converted.

### Resolution

Most areas publish quarter-hour prices. Some still publish hourly prices, Great Britain publishes
half-hours, and an area can change from one to the other. The graph shows each interval as it was
published. The planner always works in quarter-hours: an hourly price becomes four equal
quarter-hours and a half-hour price two.

### When prices are published

Prices for tomorrow are published once a day, at a time that depends on the source:

| Source | Expected publication |
|---|---|
| Nord Pool through Home Assistant | about 12:45 Brussels time |
| ENTSO-E day-ahead (your key) | about 13:00 Brussels time |
| Octopus Agile (Great Britain) | about 16:00 UK time |
| Red Eléctrica PVPC (Spain, regulated) | about 20:15 Madrid time |

SpotNav knows each source's time, so it knows when to look and how long to wait. Until then only today's prices are known. SpotNav never plans on prices that are not published; see
[Waiting for tomorrow's prices](#waiting-for-tomorrows-prices).

## What you pay

A spot price has no VAT, tax or grid fee. You can add each of them in the card, and the enabled
additions are applied as:

```text
(spot price + electricity tax + grid fee) × (1 + VAT)
```

VAT is applied last because it is charged on the tax and the grid fee too. The electricity tax and
the grid fee are in the area's smallest currency unit per kWh (öre, cent, pence); VAT is a
percentage.

- **Each addition is off, the suggestion, or your own value.** All three are off on a new area, so
  the plan starts on the bare spot price. Switch on the ones that apply to you.
- **Suggestions are per area and editable.** SpotNav suggests a VAT rate and an electricity tax
  where they are known, and a grid fee for a few areas (the Swedish ones). An area without a suggestion leaves the
  field for you to fill in. A suggested zero is a real value: northern Norway (NO4) has no VAT.
- **Grid fees are yours to check.** They depend on your network operator and your agreement, and a
  suggestion is only a typical figure.
- **Fixed monthly charges are not included**: subscription and standing charges are not part of a
  plan or a charge's cost.
- **Included in the price.** Where the published price already contains a part of the bill, that
  part is shown as *Included in the price*, checked and locked, and nothing is added for it again.
  Spain's PVPC includes the network charges (grid fee), so VAT is still added. Great Britain's Agile
  price includes VAT, tax and network charges, so nothing is added.

Your choices are kept per area, so switching areas and back keeps what you entered. Check the values
against your contract. The cost of past charges in the charge history is recalculated with your
current values every time it is shown (see [Charge history](card.md#charge-history)).

## How the integration uses the prices

The same prices, with the same fees, are there for automations that have nothing to do with a car:
see [The electricity price](price-device.md).

### Plans only on published prices

A plan is placed only in intervals that have a published price. With no departure time the plan
covers the priced horizon: today, and tomorrow once it is published.

### Waiting for tomorrow's prices

When a departure needs hours that are not priced yet (for example a 07:00 departure before the
afternoon publication), SpotNav works out how much of the charge can still be done after the
publication, at the charger's rate with a margin, before the departure.

- If all of it fits, nothing is bought now. The card says *Waiting for tomorrow's prices (~13:45),
  will plan then.*
- If not all of it fits, only the part that cannot wait is bought now, in the cheapest published
  intervals, and the rest is planned when the prices arrive. The card says *Buying 12 kWh now, the
  rest when the prices are published.*
- If the prices are late and the latest safe start arrives, the remainder is charged at once. The
  departure is kept; only the price is given up. A plan bought without published prices is flagged.

The expected publication is the area's own publication time plus a 45-minute margin, worked out
in that time's zone, so a clock change cannot shift it. An area that states no time is expected at
13:00 Brussels time. Until the expected time the card says when the prices are due ("Waiting for
tomorrow's prices (~16:45)" in Great Britain); after it, the card names no time and SpotNav treats the
prices as able to arrive at any moment.

### The history profile

For a departure on a particular day ("Sun 4 Oct", up to seven days ahead) or on chosen weekdays, the
plan can reach past the last published price. SpotNav keeps a profile of the last four weeks for
each area, built in Home Assistant from the day prices it has stored: the median price and its spread
for every weekday and hour. It compares the cheapest published intervals with what the profile
expects for the hours that are not published yet.

If the unpublished hours were clearly cheaper (by more than one standard deviation of the spread,
after your tax, grid fee and VAT), it waits and says so: *Waiting for Sunday's prices (at 13:00), which have
been 30 % cheaper over the last 4 weeks*. It only waits while the charge can still be finished in time, buys what cannot wait, and
plans again each time prices are published. The profile only ever argues for waiting; what is
charged is always decided on published prices. Without a usable profile, or without a clear saving,
it plans on the published prices.

For an ordinary daily departure the profile only explains a wait for tomorrow's prices, or ends it
when the published hours are clearly cheaper than the expected unpublished ones.

The profile needs at least 21 of the last 28 days stored; until then there is no profile and the plan
uses the published prices. After you add a source, SpotNav fills the missing past days from it in the
background, one request at a time and two seconds apart, so a profile is usually there within minutes.
With an ENTSO-E key it then also fills a year of past days, for statistics, one request every two minutes.

### Fetching and caching

There is one price cache per Home Assistant instance, shared by every charger: two chargers on the
same area make one set of requests.

- Today's prices are asked for when they are missing, and tomorrow's from 5 minutes before the source's
  expected publication, until they arrive. Once both days are held, a check asks nothing.
- It checks every 30 minutes, every 5 minutes from 5 minutes before an area's expected publication until
  two hours after it while that area's tomorrow is still missing, and every hour once tomorrow is in hand.
  Each interval varies by ±10 % so installations do not ask at the same moment. After a failure it backs off
  from one minute up to 30 minutes.
- The area list comes with SpotNav itself; it is never fetched.
- Everything fetched is stored in Home Assistant, so after a restart the last prices are there at once.
  Day prices are kept for 400 days and then dropped, so the stored file stays a bounded size. Days stored
  before 2.0 (fetched from the SpotNav relay) are kept the same way and count for the profile and the
  charge history.

### When a source cannot be reached

The last good prices are kept and never relabelled as fresh. A plan already calculated is kept as it
stands, and the card says *The prices are stale: the last successful fetch is still being used.*
Nothing new is calculated from stale data. Once the source answers again, the prices and the plan update
by themselves. A source that is there but failing (Nord Pool unreachable, ENTSO-E asking to wait) shows no
notice: there is nothing to do but wait.

## The day document

This part is technical. Every source's day is kept as one JSON document per area and market day, in
one format, whatever the source. It is the format the app reads through the webhook's `price_days`
([API](api.md#charger-webhook)) and the format the SpotNav relay published before 2.0, so a reader of
those files reads these unchanged.

### A day

```json
{
  "v": 1,
  "area": "SE3",
  "date": "2026-10-05",
  "tz": "Europe/Stockholm",
  "start": "2026-10-05T00:00:00+02:00",
  "res": 15,
  "unit": "EUR/kWh",
  "prices": [0.00501, 0.00495, 0.003, 0.00271],
  "fx": { "SEK": 11.29 },
  "fx_date": "2026-10-02",
  "fx_src": "ECB",
  "src": "entsoe.eu",
  "published": "2026-10-04T13:06:23+02:00",
  "retrieved": "2026-10-04T13:06:23+02:00"
}
```

The real file has 96 prices; it is cut to four here. A Great Britain day looks the same with
`"market_tz": "Europe/Paris"`, `"res": 30`, `"fx": { "GBP": 0.85033 }`, `"src": "octopus.energy"` and
`"published": null`.

| Field | Meaning |
| --- | --- |
| `v` | Always `1` for a day file. |
| `area`, `date` | The area and the market day. |
| `tz` | The day's calendar zone. A file may also carry `market_tz`; when present, that is the calendar. |
| `start` | The first interval's start, with the market calendar's offset. |
| `res` | Minutes per interval: 15, 30 or 60. It can differ between days of the same area. |
| `unit` | Always `EUR/kWh`. |
| `prices` | One price per interval. Element *n* starts at `start + n × res` minutes. There are no per-element timestamps. Prices can be negative. |
| `fx` | Exchange rates from EUR: local price = EUR price × rate. Empty for a euro area, and for an old day with no rate from near that day. |
| `fx_date`, `fx_src` | The date of the rate and where it comes from (`ECB`). Absent when `fx` is empty. |
| `src` | The source that produced the prices: `nordpool`, `entsoe.eu`, `octopus.energy` or `esios.ree.es`. |
| `published` | When the source published the prices, or `null` when the source does not say. |
| `retrieved` | When they were fetched. |

Clock changes need no special case. Walk `start + n × res` in absolute time, not local wall time: a
23-hour spring day has 92 quarter-hours and a 25-hour autumn day has 100.

The prices are what the source published, converted to EUR/kWh. They contain only what the area's
`included` says. For Great Britain Octopus's pence are converted to EUR with the ECB rate in `fx`, so
multiplying back by `fx.GBP` gives Octopus's price.

### The history profile

```json
{
  "v": 1,
  "area": "SE3",
  "tz": "Europe/Stockholm",
  "unit": "EUR/kWh",
  "generated": "2026-10-05T13:05:36+02:00",
  "from": "2026-09-08",
  "to": "2026-10-05",
  "weeks": 4,
  "hours": [
    { "weekday": 1, "hour": 0, "median": 0.01261, "std": 0.04668, "n": 16 },
    { "weekday": 1, "hour": 1, "median": 0.00916, "std": 0.04849, "n": 16 }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `tz` | The zone the hours are grouped in. |
| `generated` | When the profile was calculated. |
| `from`, `to`, `weeks` | The local dates the profile covers (the last 28 days) and how many weeks that is. |
| `hours` | One entry per ISO weekday (1 is Monday) and local hour (0 to 23). |
| `median`, `std` | The median price and the population standard deviation of that weekday and hour, in EUR/kWh. |
| `n` | How many prices went into it (four per hour for a quarter-hour area). |

A weekday and hour with too few prices is left out. An area with too little history has no profile
(`null` in `price_days`). The profile is built in Home Assistant from the stored days and recalculated as
days arrive.
