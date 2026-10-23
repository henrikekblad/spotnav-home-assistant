# Prices for other automations

SpotNav's prices are not only for the car. An **Electricity price** entry gives any automation, a
bathroom floor, a water heater, a washing machine, the same price SpotNav plans a charge against:
the spot price with your energy tax, grid fee and VAT added, in your currency per kWh.

- [Add an electricity price](#add-an-electricity-price)
- [The entities](#the-entities)
- [Example: a bathroom floor](#example-a-bathroom-floor)
- [The `spotnav.get_prices` action](#the-spotnavget_prices-action)
- [Example: a washing machine](#example-a-washing-machine)
- [How it is built](#how-it-is-built)

## Add an electricity price

Go to **Settings, Devices & services, SpotNav, Add entry** and choose **Electricity price**. Pick the
price area, then say for VAT, energy tax and grid fee whether to use the area's suggestion, a figure
of your own, or nothing. You can add several (another area, or other fees); each is a device of its
own, named **Electricity price** and its area. **Configure** on the entry changes the area and fees.

An electricity price is independent of chargers and sites. It needs no charger, and adding one
changes nothing a charger does. Its prices are fetched once per area for the whole installation, so
an electricity price in the same area as a charger asks the relay for nothing more.

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
already holds and never asks the relay.

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
