---
title: The battery plan
description: How SpotNav plans a home battery in shadow, its three modes and the nightly review.
order: 31
section: House and battery
---

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

SpotNav's own battery control can run this plan on the battery, but it is off by default and still in testing: even
switched on it only logs what it would do, unless the installation is a [beta tester](beta-testing.md) and the brand's
check has passed ([Home batteries](home-battery.md#spotnavs-own-battery-control-off-by-default-in-testing)).

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
| SolaX Modbus | Runs once the size is read or learnt | Level is read. The ratings are the BMS's allowed charge and discharge current (*Battery Charge Max Current*, *Battery Discharge Max Current* sensors, disabled in Home Assistant by default) times the battery voltage (an estimate), else they are learnt (below); GEN5 and GEN6 name them for battery 1 only. The size is the integration's *Battery Install Capacity* (GEN3) or *Battery System Installed Capacity* (GEN4 to GEN6), both disabled in Home Assistant by default: enable the one your inverter has, or the size is learnt. |
| Enphase | Runs once the ratings are learnt | Level and size are read; the ratings are learnt (below). |
| Sofar (the Sofar plugin of SolaX Modbus) | Runs once the size and the ratings are learnt | Only the level is read, for all battery inputs together. Read from the integration's source, not yet tested on a battery. |
| Growatt (the Growatt plugin of SolaX Modbus) | Runs once the size and the ratings are learnt | Only the level is read: the SPH and SPA storage inverters and the TL-XH, TL3-XH and WIT hybrids. The off-grid SPF is not read. Not yet tested on a battery. |
| FoxESS (FoxESS Modbus; also 1KOMMA5's `1K5-HI` units) | Runs once the size and the ratings are learnt | The level is read; on the H3-Pro and H3-Smart it is combined from the battery packs' levels. The size is learnt. The H1 and KH give their BMS's charge and discharge current, disabled in Home Assistant by default: once you enable *BMS Charge Rate* and *BMS Discharge Rate* the ratings are that current times the battery voltage (an estimate), else they are learnt. Read only. Not yet tested on a battery. |
| Solis (Solis Modbus) | Runs once the size is learnt | The level is read. The ratings are the BMS's charge and discharge current limits times the BMS's battery voltage (an estimate, low near full or empty, where the BMS lowers its limits). The integration's *Battery Rated Capacity* is a setting, not the BMS's own bank, so the size is learnt. Read only. Not yet tested on a battery. |
| Solinteg (the Solinteg plugin of SolaX Modbus; also M-TEC Energy Butler and Wattsonic) | Runs when the battery's BMS reports its limits | The size is read where the inverter's firmware states it (V27.52.3.0 and later), else learnt. The ratings are the BMS's allowed charge and discharge current times the battery voltage; the plan says it is an estimate. Not yet tested on a battery. |

A battery from any other brand has no adapter in SpotNav yet and gets no plan: the integrations above are the whole list. Where the integration also gives the plant's maximum output (Sigenergy does), the plan uses the lower of the battery's rating and that maximum.

**To teach SpotNav a rating that is learnt, let the battery charge or discharge at full power for 30 minutes once** (a forced charge or discharge from your inverter's own app will do); until then the plan assumes only what it has seen the battery do, which can be well under what it can.

**A size or rating the integration does not give is learnt, not asked for.** From the quarter hours SpotNav already measures, a steady charge or discharge that moves the level 20 points or more gives one estimate of the usable size (the energy that went through the cells, with the round trip counted, over the level change); the median of at least three agreeing runs is taken. The rated power is the highest power two quarter hours in a row held, so it is a floor: the battery may manage more than it has been asked for. Both are marked as estimates in the card and in the diagnostics (`learned`), never replace a value the integration reads, and the plan starts once they are known. There is no setting for either.

It also reads:

* the prices with every fee SpotNav adds as the buying price, and the spot price plus the price's *Extra for sold power* as the selling price (0 until you set it, no VAT; see [Prices](prices.md#the-suppliers-markup-and-what-a-sold-kwh-earns)),
* the house's expected load ([Forecasts](forecasts.md)),
* the solar forecast as SpotNav corrects it, once the correction has learnt, else the raw forecast,
* the charging plans already made for your cars, as a fixed load,
* the import limit (the main fuse less its margin, or the import target where you set one).
* the most the battery may charge from the grid: the same fuse cap SpotNav's battery control obeys (0 until it has learnt your house's peaks, on a small fuse). The plan grid charges no more than that; the sun's charge is never held back, and the Plan tab says when the fuse held charging back. It is today's cap applied to the whole horizon, so a plan made before the cap rose is on the safe side and heals at the next plan.

The battery's lowest level is the reserve its integration reports where SpotNav can read one; otherwise 10 % is assumed.
SpotNav reads a reserve only where it knows the integration's entity for it: SolaX has none mapped, and Huawei's needs the
installer password in the integration. Sofar's is its depth of discharge (*BatConfig: Depth of Discharge*) and Growatt's
TL-XH and TL3-XH's is *EMS Discharging Stop SOC (on grid)*; both are disabled in Home Assistant by default and are read
once you enable them. Growatt's SPH and SPA give *Load First Battery Minimum SOC*, and Solinteg *Battery SOC Min On Grid*,
which the inverter applies only while its *Battery SOC Protection On Grid* is on. FoxESS gives *Min SoC (On Grid)*, and Solis
*Overdischarge SOC* (the on-grid one).
The lowest level is said to be assumed in the card's details while it is.

**The round trip is learnt from your battery's own meters.** Until it is known the plan assumes 90 %: of each kWh
charged, 0.9 kWh comes back out. SpotNav then learns your battery's own figure, with no setting:

* It looks at windows that start and end at the same level (within 1.5 points) and in which at least 20 kWh went into
  the battery. What went in and what came out in such a window is the same energy stored once, so out over in is the
  round trip, standby use included. A window ends at the first moment the level is back (put where the level passed
  it inside a quarter hour); the next one starts there, and one not over in 14 days starts again.
* What went in and out is read from the battery's lifetime charge and discharge counters where the integration has
  them (Sigenergy, Huawei, SolarEdge, SolaX, Sofar, Solinteg, GoodWe ET, FoxESS, Victron with a battery monitor's
  history, Enphase), else from the battery's power that SpotNav measures every quarter hour. Most brands count at the
  battery (DC), as they measure its power; Enphase counts on the AC side. Energy from DC-coupled panels into the battery
  counts as charged, as it should. Growatt's charge counter counts only the grid's share, so it is not used.
* A counter that falls or jumps faster than the battery could charge (a reset, a wrap) spoils the window it falls in;
  so does a quarter hour that was not measured. Where both the counters and the measured power are known they must
  agree within 15 %, or the window teaches nothing: a counter that counts something else is not believed.
* A window's round trip must lie between 60 % and 98 %. The learnt value is the median of the last nine windows that
  agree within 5 points, once three do. The plan follows it slowly: only when it is 2 points or more away, and by at
  most 3 points per window, so one odd window never moves the plan. The learnt size uses it too.

The card's details show it: "Round-trip efficiency: 84 %, learnt" or "assumed 90 %". The diagnostics
(`battery.plan.learned.round_trip`) list every window taken, with its dates, energies and source, kept for two years,
and the last ones thrown away and why.

**A cold battery takes less charge.** Where the integration reads the cells' temperature (Sigenergy), SpotNav shows
in the diagnostics (`battery.plan.preheat`) how the plan's coming charges would fare while the cells are cold: each
held to part of the charge power (all of it from 10 °C, half from 5 °C, a fifth from 0 °C, none below) and the energy
the plan would miss so. That is an estimate from common LFP cells, not the battery's own figure, and the plan itself
does not use it yet. Where the battery has its own heater (Sigenergy's *ESS Preheating*), SpotNav also works out the
preheating it would ask for before each planned charge, shown in the same place; it does not write it.

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
  it cycles less. It sells to the grid only when that is highly profitable, to catch a price peak: the house comes first
  (the battery keeps what the house is expected to need until the next cheap hours, the cheapest quarter of the hours
  planned), and only energy beyond that is sold, and only where each kWh earns at least **1 kr** more than keeping it is
  worth (what the stored energy is worth to the plan, the round trip's loss and the wear). The margin is one constant,
  not a setting; other currencies have about the same sum (for example 1 kr in Norway, 0.70 kr in Denmark, 10 cent in
  euro areas).
* **Max profit**: every action, with the true wear cost, including selling to the grid whenever that pays.

Choose the mode under **Mode** in the settings' **Home battery** section; **Balanced** is the default. All three are
planned side by side on the same figures (a mode is skipped when the solve would take longer than 5 seconds), so you can
see what each would have saved. While SpotNav does not control the battery, the choice only moves which row is marked as
chosen in the comparison. With [battery control](home-battery.md#spotnavs-own-battery-control-off-by-default-in-testing)
on, it decides what the battery does: **Max profit** sells from the battery to the grid whenever that pays, **Balanced**
only when it is highly profitable, and **Self-consumption** never.

### The wear cost

Each kWh through a battery wears it a little. By default the plan counts that as **15 öre per kWh each way** (charged,
and again discharged) in Sweden; other currencies have the same sum at the rates of November 2026 (for example 16 øre
in Norway, 10 øre in Denmark, 1.4 cent in euro areas). The wear steers the plan away from pointless cycling. It is
reported separately and never counted as money you spent.

An administrator can change it: tap **Mode** in the settings' **Home battery** section, and the mode dialog has a
**Wear** field in the area's minor unit per kWh each way, from 0 up to ten times the default. A higher figure makes the
plan cycle the battery less, a lower one more. Empty the field, or choose **Default**, to go back to the currency's
figure. Balanced still counts twice the figure you set.

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
