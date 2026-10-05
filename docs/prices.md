# Prices

Where SpotNav's prices come from, what is added to them, and how the integration uses them. The
second half describes the files the SpotNav Relay publishes, for anyone who wants to read them
directly.

- [Where prices come from](#where-prices-come-from)
- [What you pay](#what-you-pay)
- [How the integration uses the prices](#how-the-integration-uses-the-prices)
- [The relay's published files](#the-relays-published-files)

## Where prices come from

Prices come from the SpotNav Relay ([spotnav.sensnology.se](https://spotnav.sensnology.se)), which
collects them once for everyone and publishes them with ECB exchange rates. There is no account and
no API key.

- **Day-ahead spot prices** from the ENTSO-E Transparency Platform for 26 countries: the Nordic and
  Baltic countries, Germany/Luxembourg, the Netherlands, Belgium, France, Austria, Switzerland,
  Poland, Czechia, Slovakia, Hungary, Slovenia, Croatia, Romania, Bulgaria, Greece, Italy (all
  zones), Spain and Portugal.
- **Spain – PVPC (regulated)** from Red Eléctrica, with the network charges already in the price.
- **Great Britain – Octopus Agile** in all 14 regions, with VAT and network charges already in the
  price. These are your prices only if you are on Agile; see
  [Great Britain](supported.md#great-britain-octopus-agile).

You choose the price area in the card (**Settings, Price area and taxes**; see [the card](card.md#price-area-and-fiscal)). A country with several
bidding zones (Sweden, Norway, Denmark, Italy) has one area per zone. The card names each area's
price source, linked, under the area.

### Currency

The relay publishes every price in euro per kWh, together with the ECB reference rate for the area's
currency and the date of that rate. Home Assistant converts with that rate, the one in the day's own
file, and shows the price in the area's currency: kronor and öre in Sweden, pounds and pence in Great
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
| ENTSO-E day-ahead (most areas) | about 13:00 Brussels time |
| Octopus Agile (Great Britain) | about 16:00 UK time |
| Red Eléctrica PVPC (Spain, regulated) | about 20:15 Madrid time |

Each area states its own expected time in the relay's area list (`publication`), so SpotNav knows
when to look and how long to wait. Until then only today's prices are known. SpotNav never plans on prices that are not published; see
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
- **Suggestions are per area and editable.** The relay suggests a VAT rate and an electricity tax
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

### Plans only on published prices

A plan is placed only in intervals that have a published price. With no departure time the plan
covers the priced horizon: today, and tomorrow once it is published.

### Waiting for tomorrow's prices

When a departure needs hours that are not priced yet (for example a 07:00 departure before the
afternoon publication), SpotNav works out how much of the charge can still be done after the
publication, at the charger's rate with a margin, before the departure.

- If all of it fits, nothing is bought now. The card says *Waiting for tomorrow's prices (~14:00),
  will plan then.*
- If not all of it fits, only the part that cannot wait is bought now, in the cheapest published
  intervals, and the rest is planned when the prices arrive. The card says *Buying 12 kWh now, the
  rest when the prices are published.*
- If the prices are late and the latest safe start arrives, the remainder is charged at once. The
  departure is kept; only the price is given up. A plan bought without published prices is flagged.

The expected publication is the area's own `publication` time plus a 45-minute margin, worked out
in that time's zone, so a clock change cannot shift it. An area that states no time is expected at
13:00 Brussels time. Until the expected time the card says when the prices are due ("Waiting for
tomorrow's prices (~16:45)"); after it, SpotNav treats the prices as able to arrive at any moment.

### The history profile

For a departure on a particular day ("Sun 4 Oct", up to seven days ahead) or on chosen weekdays, the
plan can reach past the last published price. The relay publishes a profile of the last four weeks
for each area: the median price and its spread for every weekday and hour. SpotNav compares the
cheapest published intervals with what the profile expects for the hours that are not published yet.

If the unpublished hours were clearly cheaper (by more than one standard deviation of the spread,
after your tax, grid fee and VAT), it waits and says so: *Waiting: Sundays were 30 % cheaper the last
4 weeks*. It only waits while the charge can still be finished in time, buys what cannot wait, and
plans again each time prices are published. The profile only ever argues for waiting; what is
charged is always decided on published prices. Without a usable profile, or without a clear saving,
it plans on the published prices.

For an ordinary daily departure the profile only explains a wait for tomorrow's prices, or ends it
when the published hours are clearly cheaper than the expected unpublished ones.

SpotNav asks for an area's profile at most once a day and does not use one that is more than two
days old.

### Fetching and caching

There is one price cache per Home Assistant instance, shared by every charger: two chargers on the
same area make one set of requests.

- The relay's index says which days exist. SpotNav fetches a day only after the index lists it, so
  it does not ask for days that are not published yet.
- It reads the index every 30 minutes, every 5 minutes from 5 minutes before an area's expected
  publication until two hours after it while that area's tomorrow is still missing, and every hour
  once tomorrow is in hand. Each interval varies by ±10 %
  so installations do not ask at the same moment. After a network failure it backs off from one
  minute up to 30 minutes.
- The area list is read again once a day, and at once when the index says it has changed.
- Everything fetched is stored in Home Assistant, so after a restart the last prices are there
  before the relay answers.

### When the relay cannot be reached

The last good prices are kept and never relabelled as fresh. A plan already calculated is kept as it
stands, and the card says *The prices are stale: the last successful fetch is still being used.*
Nothing new is calculated from stale data. If the area list cannot be fetched, the card's area picker
shows the last list Home Assistant received, with a note. Once the relay answers again, the prices
and the plan update by themselves.

## The relay's published files

This part is technical. It describes what a client fetches from
`https://spotnav.sensnology.se` and what the files contain. The files are public static JSON. They
need no key or account and are served with CORS open to any origin. They are the contract SpotNav's
own clients read; if you build on them, follow the [versioning](#versioning) rules and cache as the
headers say.

| Path | What it is | `Cache-Control` |
| --- | --- | --- |
| `/v2/areas.json` | Every area: identity, time zones, currency, suggestions, source | `public, max-age=3600, must-revalidate` |
| `/v2/index.json` | Which recent days exist for each area, and their resolution | `public, max-age=30, must-revalidate` |
| `/v1/{area}/{YYYY}/{MM-DD}.json` | One area's prices for one day | `public, max-age=604800, immutable` |
| `/v1/{area}/profile.json` | Four weeks of prices by weekday and hour | `public, max-age=300, must-revalidate` |
| `/v1/areas.json`, `/v1/index.json` | The v1 area list and index, frozen | as their v2 counterparts |

There are no `/v2/` day or profile paths: day files and profiles keep their `/v1/` paths under both
contract versions.

### How to read it

1. Read `/v2/areas.json` to learn the areas.
2. Read `/v2/index.json` to learn which days each area has. It is small and cheap to revalidate.
3. Fetch a day only once the index lists it. A published day never changes, so cache it for good.

Do not poll a day URL waiting for it to appear; poll the index. The index carries `areas_rev`, so a
client also notices a changed area list without polling `areas.json` often.

### `/v2/areas.json`

```json
{
  "v": 2,
  "generated": "2026-10-05T20:22:35+02:00",
  "areas": [
    {
      "id": "ES-PVPC",
      "countries": ["ES"],
      "name": "Spain – PVPC (regulated)",
      "tz": "Europe/Madrid",
      "currency": "EUR",
      "major_unit": "€",
      "minor_unit": "cent",
      "included": ["grid_fee"],
      "vat_percent": 21,
      "source": { "name": "Red Eléctrica (ESIOS) – PVPC", "url": "https://www.esios.ree.es/es/pvpc" }
    },
    {
      "id": "GB-C",
      "countries": ["GB"],
      "name": "GB C – London",
      "tz": "Europe/London",
      "market_tz": "Europe/Paris",
      "currency": "GBP",
      "major_unit": "£",
      "minor_unit": "p",
      "included": ["vat", "tax", "grid_fee"],
      "source": { "name": "Octopus Energy (Agile)", "url": "https://octopus.energy/smart/agile/" }
    },
    {
      "id": "SE3",
      "eic": "10Y1001A1001A46L",
      "countries": ["SE"],
      "name": "Stockholm",
      "tz": "Europe/Stockholm",
      "currency": "SEK",
      "major_unit": "kr",
      "minor_unit": "öre",
      "vat_percent": 25,
      "suggested_tax": 36,
      "suggested_grid_fee": 30,
      "publication": { "time": "13:00", "tz": "Europe/Brussels" },
      "source": { "name": "ENTSO-E Transparency Platform", "url": "https://transparency.entsoe.eu/" }
    }
  ]
}
```

Areas are sorted by `id`. Only areas the relay can serve are listed.

| Field | Meaning |
| --- | --- |
| `id` | The area's id, `[A-Z0-9-]`, up to 32 characters: `SE3`, `DE-LU`, `GB-C`, `ES-PVPC`. |
| `eic` | Optional. The ENTSO-E area code. A Great Britain region and `ES-PVPC` have none. |
| `countries` | ISO country codes the area covers. |
| `name` | One neutral display name, not a translation. |
| `tz` | The IANA time zone people read the times in. |
| `market_tz` | Optional; absent means `tz`. The zone whose calendar day one day file covers. Great Britain is `Europe/Paris`, because Agile runs 23:00 to 23:00 UK time. Portugal is `Europe/Madrid`. |
| `currency` | ISO 4217 code. Use this, not the unit labels, for arithmetic and exchange rates. |
| `major_unit`, `minor_unit` | Display labels for the whole and the hundredth unit (`kr`/`öre`, `€`/`cent`, `£`/`p`). Not unique: SEK, NOK and DKK are all `kr`. Display them; do not parse them. |
| `vat_percent` | Optional. Suggested VAT in percent. |
| `suggested_tax` | Optional. Suggested electricity tax, in the minor unit per kWh. |
| `publication` | Optional. `{ "time": "HH:MM", "tz": "<IANA zone>" }`: when tomorrow's prices are expected, in that zone (`13:00` `Europe/Brussels` for ENTSO-E). A client that does not know it, or an area without it, assumes 13:00 Brussels. |
| `suggested_grid_fee` | Optional. Suggested grid fee, in the minor unit per kWh. |
| `included` | Optional; absent means `[]`. Which of `vat`, `tax` and `grid_fee` the published price already contains. A client applies none of those. |
| `source` | `{ "name", "url" }`: where the prices come from, for attribution. |

The suggestions are best effort. An area without a known figure omits the field, and zero and absent
are different facts: `"vat_percent": 0` means there is no VAT; no field means it is not known.

### `/v2/index.json`

```json
{
  "v": 2,
  "generated": "2026-10-05T20:22:35+02:00",
  "res_default": 60,
  "areas_rev": "4d3df4fb93ba",
  "areas": {
    "CH": { "days": ["2026-10-04", "2026-10-05", "2026-10-06"] },
    "GB-C": { "days": ["2026-10-04", "2026-10-05", "2026-10-06"], "res": 30 },
    "SE3": { "days": ["2026-10-04", "2026-10-05", "2026-10-06"], "res": 15 }
  }
}
```

| Field | Meaning |
| --- | --- |
| `generated` | When the index was written. |
| `res_default` | The resolution in minutes for an area that states none. |
| `areas_rev` | The first twelve hex digits of the SHA-256 of `/v2/areas.json`. When it changes, read the area list again. The area list is always written before the index that points at it. |
| `areas.{id}.days` | The recent days that exist: yesterday, today and tomorrow, on the area's market calendar. Older days are not listed but stay at their URLs. |
| `areas.{id}.res` | Optional; absent means `res_default`. The resolution in minutes: 15, 30 or 60. |

### A day: `/v1/{area}/{YYYY}/{MM-DD}.json`

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
| `src` | The source that produced the prices: `entsoe.eu`, `octopus.energy` or `esios.ree.es`. |
| `published` | When the source published the prices, or `null` when the source does not say. |
| `retrieved` | When the relay fetched them. |

Clock changes need no special case. Walk `start + n × res` in absolute time, not local wall time: a
23-hour spring day has 92 quarter-hours and a 25-hour autumn day has 100.

The prices are what the source published, converted to EUR/kWh. They contain only what the area's
`included` says. For Great Britain the relay converts Octopus's pence to EUR with the ECB rate in
`fx`, so multiplying back by `fx.GBP` gives Octopus's price.

### The history profile: `/v1/{area}/profile.json`

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

A weekday and hour with too few prices is left out. An area with too little history has no profile,
and the relay answers 404. A profile is recalculated as days arrive, so it is served with a short
cache time, unlike a day.

### What a 404 means

A 404 means the file does not exist yet, or not at all: a day that is not published, a profile there
is not enough history for, or an area that does not exist. It is cached for only 60 seconds, so a
client can ask again soon, but it is better to wait until the index lists the day. The body of a 404
is not JSON.

### Versioning

The path segment (`/v1/`, `/v2/`) and the `"v"` field change together. A change that could break a
reader gets a new version, served alongside the old one. Adding a field is not such a change, so a
reader must ignore fields it does not know.

- **v1** (`/v1/areas.json`, `/v1/index.json`) is frozen. It lists the ENTSO-E bidding zones in the v1
  shape and never gains a field.
- **v2** (`/v2/areas.json`, `/v2/index.json`) lists every area, including those v1 does not (Great
  Britain, `ES-PVPC`), and adds `market_tz`, `included`, `source`, an optional `eic` and 30-minute
  days.
- **Day files and profiles** have one path and `"v": 1` under both. A day for a v2-only area may
  carry v2 fields such as `market_tz`.

A client reads v2 first and falls back to v1 when v2 is absent. It skips an area entry it cannot use
(an unknown `included` value, a resolution it cannot plan with) instead of refusing the whole list.

The integration does exactly this (`custom_components/spotnav/pricing/relay_contract.py` and
`price_repository.py`): it reads `/v2/` first and falls back to `/v1/` when the relay answers v2 with
a 404 or a body it cannot read, and it reads the index in the same version as the area list it holds.
Every document is checked against the contract, and one that fails is never used in place of the last
good one.
