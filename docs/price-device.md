# The electricity price

An **Electricity price** is where SpotNav keeps your price area, VAT, energy tax and grid fee. Every
charger plans its charges against it, and any automation, a bathroom floor, a water heater, a washing
machine, can use the same price: the spot price with your energy tax, grid fee and VAT added, in your
currency per kWh.

- [One price for the whole installation](#one-price-for-the-whole-installation)
- [Several electricity prices](#several-electricity-prices)
- [Upgrading from an earlier version](#upgrading-from-an-earlier-version)
- [The entities](#the-entities)
- [Example: a bathroom floor](#example-a-bathroom-floor)
- [The `spotnav.get_prices` action](#the-spotnavget_prices-action)
- [Example: a washing machine](#example-a-washing-machine)
- [How it is built](#how-it-is-built)

## One price for the whole installation

You do not add it yourself. When the first charger or site is added, SpotNav makes the electricity
price from Home Assistant's country and location, with the area's suggested fees, without asking
anything. Every charger and site added later uses it too. With no country or a location SpotNav
cannot place, nothing is made; choosing the area in the card then makes it.

The area and fees are changed in one place: the card's **Electricity price** section (see
[the card](card.md#electricity-price)), the device's own entities, or **Configure** on the entry
(**Settings, Devices & services, SpotNav**). A change applies at once to every charger that uses the
price; each of them plans again. The card names the price and says how many chargers use it
("Electricity price SE4 · used by all 2 chargers").

A site points at one electricity price, and all its chargers use it. A charger without a site
points at one itself. While there is only one electricity price, every charger uses it.

## Several electricity prices

A second price is for a second address (another area or grid company) or a charger whose fees
differ, such as a company charger without VAT. Add one under **Settings, Devices & services,
SpotNav, Add entry, Electricity price**: pick the price area, then say for VAT, energy tax and grid
fee whether to use the area's suggestion, a figure of your own, or nothing. Each is a device of its
own, named **Electricity price** and its area; you can rename it.

With more than one, the card's **Electricity price** section has a **Price from** row where an
administrator chooses which one the charger uses. For a charger on a site, the choice is the site's:
every charger on it follows. Its prices are fetched once per area for the whole installation, so a
second electricity price in the same area asks the source for nothing more. Where the prices come from
(Nord Pool, PVPC or Octopus Energy through Home Assistant, or your own ENTSO-E key) is described in
[Prices](prices.md#where-prices-come-from); the device's problem names what to add when its area has none.

## Upgrading from an earlier version

Before 1.22.0 each charger kept its own area and fees, and an electricity price device was separate
from them. On the first start of 1.22.0, once, SpotNav moves each charger's area and fees to an
electricity price:

- Chargers with the same area and fees share one. An existing electricity price with exactly that
  area and those fees is used; otherwise one is made.
- A site whose chargers all had the same fees points at that one. Chargers on a site whose fees
  differ each point at their own.
- An existing electricity price with the same area but other fees is never changed. The chargers get
  one of their own, named after who uses it: the site ("Electricity price SE4 (Home)"), else the
  charger, else how many chargers. The card asks once: "Two electricity prices for SE4 with different fees. Use
  one?" **Use …** moves every charger and site that used the other one to the chosen one; the other
  stays and can be removed under **Devices & services**. **Keep both** keeps them as they are. The
  question is not asked again either way. Only administrators see it.
- Each charger's own entities for the area and fees (the price area and fee policy selects, the VAT,
  energy tax and grid transfer fee numbers) are removed; the electricity price has them now. An
  automation that used the charger's entities needs the electricity price's.
- The chargers' own stored figures are kept as they were, so a past charge is priced as it was, and
  going back to 1.21 prices exactly as before.

With the area SE4 the entities are named `sensor.electricity_price_se4_price_now`,
`binary_sensor.electricity_price_se4_cheap_now`, `binary_sensor.electricity_price_se4_expensive_now`,
`number.electricity_price_se4_cheap_hours_per_day` and `number.electricity_price_se4_expensive_hours_per_day`
(in Swedish Home Assistant the names read *Elpris nu*, *Billigt nu* and *Dyrt nu*; the entity ids
follow the device's name, which you can change).

## The entities

| Entity | What it is |
|---|---|
| **Price now** (sensor) | The price of the current quarter-hour (or hour, where the area publishes hourly) with fees and VAT, in the area's currency per kWh (for example kr/kWh). It changes exactly at each interval's start and is unavailable when there is no price for now. |
| **Cheap now** (binary sensor) | On during the day's cheapest hours. How many hours is the **Cheap hours per day** number (default 6). |
| **Expensive now** (binary sensor) | On during the day's most expensive hours. How many is the **Expensive hours per day** number (default 4). |
| **Cheap hours per day**, **Expensive hours per day** (numbers) | 0 to 24 in quarter-hours. They keep their value across restarts. |
| **Price area** (select), the three fee policies (selects) and **VAT**, **Energy tax**, **Grid transfer fee** (numbers) | The area and fees this electricity price uses. A change takes effect at once, for every charger that prices with it. |

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

## Example: a bathroom floor

Warmer while power is cheap, cooler while it is dear, and the normal setpoint in between:

```yaml
automation:
  - alias: "Bathroom floor follows the price"
    triggers:
      - trigger: state
        entity_id:
          - binary_sensor.electricity_price_se4_cheap_now
          - binary_sensor.electricity_price_se4_expensive_now
      - trigger: homeassistant
        event: start
    actions:
      - action: climate.set_temperature
        target:
          entity_id: climate.bathroom_floor
        data:
          temperature: >
            {% if is_state('binary_sensor.electricity_price_se4_cheap_now', 'on') %}26
            {% elif is_state('binary_sensor.electricity_price_se4_expensive_now', 'on') %}20
            {% else %}23{% endif %}
```

Set **Cheap hours per day** and **Expensive hours per day** on the device to how long the floor may
run warm and how long it should hold back.

## The `spotnav.get_prices` action

`spotnav.get_prices` answers with the prices of one electricity price entry, for an automation or a
script to work with. It changes nothing, so every user may call it. It reads only what Home Assistant
already holds and never asks a source.

| Field | |
|---|---|
| `config_entry_id` | The electricity price entry. Can be left out when there is only one. |
| `device_id` | The electricity price's device, instead of the entry. |
| `start`, `end` | The span to answer for. Without them: today, and tomorrow once it is published. A time without a zone is Home Assistant's local time. At most seven days. |

The answer follows Home Assistant's own Nord Pool action: a list of intervals under the area's id, with
the currency, unit and resolution beside it. `price` is with your fees and VAT, `spot_price` without;
both in the area's currency per kWh, four decimals.

```yaml
config_entry_id: 01K8...
area: SE4
currency: SEK
unit: kr/kWh
resolution_minutes: 15
tomorrow_published: true
SE4:
  - start: "2026-09-22T00:00:00+02:00"
    end: "2026-09-22T00:15:00+02:00"
    price: 3.4339
    spot_price: 2.0871
  - start: "2026-09-22T00:15:00+02:00"
    end: "2026-09-22T00:30:00+02:00"
    price: 3.339
    spot_price: 2.0112
  # ... one row per interval, 96 a day (92 or 100 on a clock-change day)
```

## Example: a washing machine

A script that finds the cheapest two hours from now on, and an automation that starts the machine
then (a machine with a smart plug, loaded and set in the evening):

```yaml
input_datetime:
  washing_machine_start:
    name: Washing machine start
    has_date: true
    has_time: true

script:
  washing_machine_plan:
    alias: "Plan the washing machine"
    sequence:
      - action: spotnav.get_prices
        response_variable: prices
      - variables:
          best: >
            {% set rows = prices[prices.area] %}
            {% set n = (120 / prices.resolution_minutes) | int %}
            {% set ns = namespace(start=none, cost=none) %}
            {% for i in range(rows | count - n + 1) %}
              {% if as_datetime(rows[i].start) >= now() %}
                {% set cost = rows[i:i + n] | sum(attribute='price') %}
                {% if ns.cost is none or cost < ns.cost %}
                  {% set ns.start = rows[i].start %}
                  {% set ns.cost = cost %}
                {% endif %}
              {% endif %}
            {% endfor %}
            {{ ns.start }}
      - action: input_datetime.set_datetime
        target:
          entity_id: input_datetime.washing_machine_start
        data:
          timestamp: "{{ as_timestamp(best) }}"

automation:
  - alias: "Start the washing machine when it is cheapest"
    triggers:
      - trigger: time
        at: input_datetime.washing_machine_start
    actions:
      - action: switch.turn_on
        target:
          entity_id: switch.washing_machine
```

Run the script after tomorrow's prices are published (in the afternoon) to let it look into the night.

## How it is built

- Each charger finds its price through `pricing/price_source.py`: its own pointer (`price_entry_id` in its
  entry data), else its site's, else the only electricity price, else its own stored settings. Pointers are
  written by the first install, the one-time migration, the card's **Price from** and its question
  (`pricing/price_setup.py`); the migration's marker and the same-area records are in the
  `spotnav.price_sources` store (`pricing/price_store.py`). A price entry's revision is part of a plan's
  identity, so a fee change replans every charger that uses it.
- A kind of config entry, `price`, beside `charger` and `site`. Its data is the area and the
  three fees, each stored as the charger's settings store them (`FiscalOverride`: off, on with the
  area's suggestion, or on with a figure). The options flow, the device's area and fee entities and a
  charger's settings writes (the card, the app) edit the same data in place
  (`price_source.async_write_price_entry`): the entry is not reloaded, its device prices again at once.
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
