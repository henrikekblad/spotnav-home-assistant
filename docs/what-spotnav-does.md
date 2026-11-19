---
title: What SpotNav does
description: Charging, the house, the home battery, heating, hot water and solar. What each part does, what it needs, and whether it acts or runs in shadow.
order: 3
---

# What SpotNav does

SpotNav plans each part of your home on the same prices and the same forecasts, under the same main fuse. A part
that controls a device runs in **shadow** first: it shows what it would do and what that would have saved, and
writes nothing until you turn it on and a check has passed.

## EV charging

**Acts.** SpotNav starts and stops your charger and sets its current.

- Tell it the energy or the charge level you want and when you leave. It charges in the cheapest quarter-hours
  before that time, fees and tax included.
- It learns your car's real charging power and how much energy one percent of its battery takes.
- With a site, it keeps every phase under your main fuse and shares the room between chargers.
- With several cars at one charger, it works out which car is plugged in and asks only when it cannot tell.

Read more: [The charging card](card.md), [Charging strategies](strategies.md),
[Target state of charge](target-soc.md), [Which car is plugged in?](vehicle-identification.md),
[Site and load balancing](site-and-load-balancing.md).

## The house

**Shows only.** The house card writes to nothing.

- Live flows between the sun, the grid, the battery, the house, the car and the heat.
- A forecast of the house's own load for the next two days, learned from your meter and the outdoor temperature.
- What the battery earned and what SpotNav saved, per day, month and year.

Read more: [The house card](house-card.md), [Forecasts](forecasts.md).

## Home battery

**Shadow by default.** Control is opt-in and still in testing.

- SpotNav plans the battery for the next day or two: when to charge from the grid, when to hold, when to cover the
  house. You choose the mode: **Self-consumption**, **Balanced** (the default) or **Max profit**.
- Each night it compares the plan with what the battery really did. If your battery has its own optimiser (such as
  Sigenergy's AI mode), this is a benchmark against it.
- **Battery control** lets SpotNav run the plan on the battery. It starts only after a check that the battery obeys
  every command. No brand is marked as field-tested yet, so control stays in test mode (it logs what it would do)
  for everyone except [beta testers](beta-testing.md).
- On plants that take a power target per phase, a grid charge can use each phase's own room under the fuse.
- Without control, SpotNav can still tell another planner (Predbat, EMHASS, your own automation) when the car charges
  from the grid.

Read more: [Home batteries](home-battery.md), [The battery plan](battery-plan.md).

## Heating

**Shadow by default.** Control is opt-in.

- The heat card shows the wanted temperature, **Home** or **Away**, the coldest room and the heat plan for the day.
- SpotNav plans to lower the heat in expensive hours, within fixed comfort rules: never for more than 3 hours in a
  row, at most 6 hours a day, never at −15 °C or colder outdoors, and released at once when a room falls below the
  comfort band.
- **Control the heat** turns control on. SpotNav can steer through a relay on the pump's input, SG Ready, or, on some
  pumps, the pump's own integration. A short check runs first. It never writes a setpoint.
- The paths through the pump's own integration have not yet run on a real pump.

Read more: [Heat pumps](heat-pumps.md).

## Hot water

**Shadow by default.** Goes live only behind **Control the heat**, and only after its own gate.

- SpotNav learns when your household uses hot water and plans the tank's reheat into the cheap hours before it.
- In shadow it shows each planned run and what it would have saved.
- It starts steering after at least a week of shadow in which it hit at least 70 % of your peaks and saved money.
- Where the pump can start a hot-water run that ends by itself, the heat card has a **Hot water now** button. It
  works from the start and is yours: SpotNav never stops it.

Read more: [Hot water](hot-water.md).

## Solar

**Acts for charging.** The forecasts behind it run in the background.

- The **Solar** strategy charges the car from surplus only. **Hybrid** mixes the sun with cheap grid hours and holds
  back when the forecast promises sun.
- SpotNav corrects your solar forecast against what your panels really produced.
- SpotNav also makes its own solar forecast. It is logged and scored each night but does not feed any plan yet.

Read more: [Solar](solar.md).

## What SpotNav never does

- It never takes over a device that another controller runs. It watches and says so.
- It never skips a safety rule: the fuse, the write limits, the comfort band and the read-back of every write.
- It never stops a boost or a charge you started yourself.
- When it stops controlling something, it puts back what it found.
