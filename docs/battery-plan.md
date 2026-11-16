# The battery plan (shadow)

SpotNav 3.0 plans your home battery for the next day or two and compares that plan with what the battery really does. **It
only watches.** Nothing in this page writes to a battery or an inverter: the plan is a benchmark you can read in the
[house card](house-card.md) and in the site's diagnostics. If you want something that acts on a battery today, see
[Home batteries](home-battery.md) (the "charging from the grid" signal).

## What is shadow and what acts

| Part | Today |
| --- | --- |
| The battery plan, the three modes, the nightly review, the stored value | Shadow: computed and shown, never sent to the battery |
| Holding the battery while a car charges from the grid | Shadow: SpotNav logs when it would have held it and writes nothing |
| Noticing that something else drives the battery | Only noticed and logged: the battery is marked watch-only in SpotNav's own view |
| The "charging from the grid" signal, the chargers and load balancing | These act, as before, and never write to the battery |

No part of SpotNav can be switched on to write to a battery in this version. Whether it ever will is not decided here.

## What the plan needs

The plan runs on a site whose battery SpotNav can read: its charge level, its usable size in kWh and its rated charge and
discharge power. Without any one of those figures there is no plan, and the diagnostics say which is missing. What each
brand's integration offers:

| Integration | Plan | Notes |
|---|---|---|
| Sigenergy, Huawei Solar, SolarEdge Modbus Multi | Runs | Level, size and both ratings are read as they are. |
| Victron (GX, the HACS Victron MQTT) | Runs when the battery's BMS reports its limits | The size is the BMS's installed Ah times the system voltage, the ratings its allowed charge and discharge current times the voltage; the plan says it is an estimate. A battery without a CAN-bus BMS (lead-acid, a plain shunt) gives no ratings and so no plan. |
| Victron (community Modbus integration) | Runs when the BMS reports its limits | The size is the Dynamic ESS "battery capacity" where it is filled in, else the capacity register times the voltage. |
| GoodWe | Runs once the size is learnt | The integration reads the level and the BMS's current limits but not the battery's size; SpotNav learns it (below). |
| SolaX Modbus | Runs once the ratings are learnt | Level and size are read; the ratings are learnt (below). |
| Enphase | Runs once the ratings are learnt | Level and size are read; the ratings are learnt (below). |

A battery from any other brand has no adapter in SpotNav yet and gets no plan: the integrations above are the whole list. Where the integration also gives the plant's maximum output (Sigenergy does), the plan uses the lower of the battery's rating and that maximum.

**A size or rating the integration does not give is learnt, not asked for.** From the quarter hours SpotNav already measures, a steady charge or discharge that moves the level 20 points or more gives one estimate of the usable size (the energy that went through the cells, with the round trip counted, over the level change); the median of at least three agreeing runs is taken. The rated power is the highest power two quarter hours in a row held, so it is a floor: the battery may manage more than it has been asked for. Both are marked as estimates in the card and in the diagnostics (`learned`), never replace a value the integration reads, and the plan starts once they are known. There is no setting for either.

It also reads:

* the prices with every fee SpotNav adds as the buying price, and the spot price plus the price's *Sold kWh: extra or
  deduction* as the selling price (0 until you set it, no VAT; see [Prices](prices.md#the-suppliers-markup-and-what-a-sold-kwh-earns)),
* the house's expected load ([Forecasts](forecasts.md)),
* the solar forecast as SpotNav corrects it, once the correction has learnt, else the raw forecast,
* the charging plans already made for your cars, as a fixed load,
* the import limit (the main fuse less its margin, or the import target where you set one).

The battery's lowest level is the reserve its integration reports where SpotNav can read one; otherwise 10 % is assumed.
SpotNav reads a reserve only where it knows the integration's entity for it: SolaX has none mapped, and Huawei's needs the
installer password in the integration.
The round trip is assumed to be 90 %. Both are said to be assumed in the card's details.

The plan is made again when the prices change (the next day's at 13:00), every hour with the forecast, when the cars' plans
or the battery's facts change and when the charge level is more than 10 points off the plan. The action of the slot that
is running changes only if the new plan is clearly better, so the plan does not flip back and forth. The plan reaches as far
as the prices do, at most two days.

## The three modes

A site has one mode: **Self-consumption**, **Balanced** or **Max profit**. They differ only in how much the battery trades
on price. Every mode keeps the battery's own lowest level.

* **Self-consumption**: no plan. The battery covers the house and stores a surplus, as the inverter does by itself. This is
  the baseline every saving is measured against.
* **Balanced** (the default): the battery may buy cheap and spend in the house. It counts twice the wear cost per kWh, so
  it cycles less, and it sells stored energy to the grid only when the price is clearly above what that energy cost to store.
* **Max profit**: every action, with the true wear cost, including selling to the grid where that pays.

There is no setting for the mode yet: **Balanced is the mode in force**. The other two are planned beside it on the same
figures (a mode is skipped when the solve would take longer than 5 seconds), so you can see what each would have saved before any of them is ever used.

### The wear cost

Each kWh through a battery wears it a little. The plan counts that as **15 öre per kWh each way** (charged, and again
discharged) in Sweden. It is one constant, not a setting; other currencies have the same sum at the rates of
November 2026 (for example 16 øre in Norway, 10 øre in Denmark, 1.4 cent in euro areas). The wear steers the plan away
from pointless cycling. It is reported separately and never counted as money you spent.

## The stored value

For the day's money SpotNav splits the day the way a battery's own app does: what was **sold**, what the battery's moves
earned (**arbitrage**) and what was **bought**. For arbitrage it needs what the energy in the battery cost, so it keeps a
running average cost of what is stored:

* energy from the **grid** is valued at the buying price of that quarter hour, every fee included,
* energy from the **sun** is valued at the **selling price** (spot plus the sold-kWh figure), what that kWh would have fetched on the grid,
* each kWh the battery gives back earns the price of that quarter hour (the buying price for what the house used, the selling
  price for what was exported) less that average cost.

The battery's losses make the stored kWh fewer for the same money, which raises the average cost of the rest; they do not
show as a loss of their own. The first day starts from the charge level and that day's mean buying price, and the card says
the figure is estimated until that energy has been used. Only quarter hours that were measured for at least half their time
and have prices count.

## The nightly review

The day after, SpotNav takes the day's measured load and sun and runs against them: what the battery really did, each mode's
plan, and plain self-consumption, all from the same measured charge level. Each is charged the change in its stored energy
at the day's end value, so a plan that empties the battery does not look cheap. The result is the saving of the plan against
self-consumption, and what the battery's own way saved against the same baseline. The last 30 days are kept.

What the review cannot tell: the simulated battery is ideal (no real losses or ramp limits), so a plan looks a few percent
better than it would be, and a quarter hour where the meters were lost counts nowhere.

If your battery runs its own optimiser (Sigen AI mode, for example), that is the point: the plan is a benchmark of what
SpotNav would have done, and the review shows which way earned more.

## Keep SpotNav's records

SpotNav keeps what it decided and measured only for a while: the battery plan's quarter hours for three days, the forecasts
and their outcomes for 14, the nightly reviews for 30. To keep a day for good, or to try a changed plan on it later, call the
action **SpotNav: Export a day's records** (`spotnav.export_day`, administrators and automations only). It writes one JSON
file per site and day to `spotnav_archive/<site id>/<date>.json` in Home Assistant's configuration folder (the folder is
made; the same day again overwrites its file) and answers with the path and what it holds.

* **Fields:** `site` (the site's entry or device; leave it out with one site) and `date` (the local day; yesterday by
  default). Today can be exported too: the file then says `"complete": false`.
* **What is in it** (`schema` 1; instants in UTC, the day's time zone in `tz`, the units in `units`): each forecast of the
  day with when it was made and what came, and each kind's review; the day's prices (spot, and buy and sell with your fees
  as they are now, the markup and the sell adjustment); the battery plan's quarter hours (the meters, the charge level and
  each mode's planned action) and the day's review with its baselines; the house's hours (import, house, cars, heat where a
  meter is, outdoor temperature); the settings the plan read; the shadow logs of the switched load and the export limit;
  and the chargers' plug-ins that day. A typical day is 50-60 kB.
* **A day no longer kept** exports what is left: `missing` names each part that has nothing and why (`not_kept`,
  `none_recorded`, `not_reviewed_yet`, `no_battery`, `no_prices`, `no_switched_load`).

Nothing runs unless the action is called. To keep every day, call it each night, after the review:

```yaml
alias: Keep SpotNav's records
triggers:
  - trigger: time
    at: "03:30:00"
actions:
  - action: spotnav.export_day
mode: single
```

With more than one site, add one action per site with `data: {site: <the site's entry id>}`.

Two tools in the integration's repository read the files:

* `tools/spotnav_archive_to_influx.py <file> --url http://<influxdb>:8181 --db spotnav --token-file <file>` writes the day
  to InfluxDB 3 (forecasts by kind and lead time, prices, the review per mode and each forecast's error). It needs only the
  Python in Home Assistant's container, so a `shell_command` can run it after the export; the token is read from a file or
  `INFLUXDB3_AUTH_TOKEN` and never printed. Writing a day again overwrites its points.
* `.venv/bin/python tools/replay_day.py <file>` re-runs the battery plan on the day with today's planner and prints its
  saving beside the recorded one (`--once` plans once at midnight, `--hindsight` on the measured house and sun).

## When something else controls the battery

SpotNav notices a planner (Predbat, EMHASS), an aggregator integration (such as CheckWatt) or a battery whose behaviour looks
driven from outside:

* its settings change three times within 24 hours without SpotNav having written any of them, or
* in plain self-consumption it charges from the grid or discharges into export, by more than 1 kW, for ten minutes in a
  row, with its level moving the same way.

It then marks the battery **watch-only**. This ends by itself 48 hours after the last sign. SpotNav raises nothing in
Settings → Repairs, because there is nothing for you to do. It writes one line to the log at INFO when it starts and one when
it stops, and keeps the event in the diagnostics (`battery.outside_control`). The plan keeps running as a benchmark.

## Where to see it

* The [house card](house-card.md) draws the plan and the money.
* The site's diagnostics (and the debug file) hold `battery.plan`: each slot's planned action, the charge level, the cost, the
  baseline, the saving and the reason for each slot, and the reviewed days.
