# Prices for other automations

SpotNav's prices are not only for the car. An **Electricity price** entry gives any automation, a
bathroom floor, a water heater, a washing machine, the same price SpotNav plans a charge against:
the spot price with your energy tax, grid fee and VAT added, in your currency per kWh.

- [Add an electricity price](#add-an-electricity-price)
- [The entities](#the-entities)
- [The `spotnav.get_prices` action](#the-spotnavget_prices-action)
- [How it is built](#how-it-is-built)

## Add an electricity price

Go to **Settings, Devices & services, SpotNav, Add entry** and choose **Electricity price**. Pick the
price area, then say for VAT, energy tax and grid fee whether to use the area's suggestion, a figure
of your own, or nothing. You can add several (another area, or other fees); each is a device of its
own, named **Electricity price** and its area. **Configure** on the entry changes the area and fees.

An electricity price is independent of chargers and sites. It needs no charger, and adding one
changes nothing a charger does.

## The entities

| Entity | What it is |
|---|---|
| **Price now** (sensor) | The price of the current quarter-hour (or hour, where the area publishes hourly) with fees and VAT, in the area's currency per kWh (for example kr/kWh). It changes exactly at each interval's start and is unavailable when there is no price for now. |
| **Cheap now** (binary sensor) | On during the day's cheapest hours. How many hours is the **Cheap hours per day** number (default 6). |
| **Expensive now** (binary sensor) | On during the day's most expensive hours. How many is the **Expensive hours per day** number (default 4). |
| **Cheap hours per day**, **Expensive hours per day** (numbers) | 0 to 24 in quarter-hours. They keep their value across restarts. |

Price now's attributes are small on purpose: the area, the currency, the resolution in minutes, the
spot price without fees, when the price next changes and whether tomorrow's prices are published.
The two binary sensors carry the day's threshold price and when they next turn on or off. None of
the attributes are recorded in the history; only the states are.

### How the hours are chosen

- Each local day of the area is ranked on its own, by the price with fees. Equal prices go to the
  earlier interval.
- The hours are counted in the area's intervals: with quarter-hour prices, 2.25 hours is the nine
  cheapest quarters. With hourly prices a fraction is rounded to the nearest whole hour (half an
  hour or more counts as one).
- 0 hours is never on; 24 hours is always on, also on the 25-hour day in October.
- The quarters are chosen one by one, so the cheap hours need not be one block.

Known limits of this first version: because each day is ranked on its own, the sensors can jump at
midnight, and a late evening is never compared with the night after it. A choice of one contiguous
block is not there yet.

## The `spotnav.get_prices` action

`spotnav.get_prices` answers with the prices of one electricity price entry, for an automation or a
script to work with. It changes nothing, so every user may call it.

## How it is built

- A new kind of config entry, `price`, beside `charger` and `site`. Its data is the area and the
  three fees, each stored as the charger's settings store them (`FiscalOverride`: off, on with the
  area's suggestion, or on with a figure). The options flow edits the same data and reloads the entry.
- The fees are resolved by the charger's own code (`auto_controller.fiscal_choice_for`) and the
  price with fees by the planner's (`planner.chart_intervals`, which applies
  `planner.effective_minor_per_kwh`), so a charger and a price entry with the same settings show the
  same number.
- Prices come from the shared price refresh manager: the entry subscribes to its area under its own
  owner (`price:<entry id>`), so it shares the one fetch per area with the chargers, and releases it
  when the entry unloads.
- One object per entry (`price_device.PriceDevice`) holds the area's intervals, ranks each day, and
  keeps one timer at the next interval boundary; the entities read it and are told when it changes.
  No entity polls.
- Time zones are read from the resolved cache (`time_zones`), never looked up on the event loop.
- The selection rule is one function with a name (`individual`), so a later `contiguous` rule can be
  added beside it.
