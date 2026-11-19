---
title: Solar
description: How SpotNav uses your solar panels and the solar forecast, for the car, the battery plan and the house.
order: 50
section: Solar and forecasts
---

# Solar

SpotNav uses the sun in three ways: it charges the car from surplus, it plans around the solar forecast, and it
learns how good that forecast is at your house.

## What you need

- A [site](site-and-load-balancing.md) with a grid meter that measures export as well as import.
- For surplus charging: the meter's **total grid power** (or per-phase power and voltage). The card says when
  something is missing.
- For planning around the sun: a solar forecast in Home Assistant's Energy dashboard, such as Forecast.Solar or
  Solcast. Choose it under the site's **Solar** settings.

## Charging the car on the sun

**Acts.** Choose the strategy on the charging card:

| Strategy | What it does |
| --- | --- |
| **Cheapest** | Ignores the sun. Charges in the cheapest hours. |
| **Solar** | Charges only from surplus, and follows it with the charger's current. |
| **Hybrid** | Plans the cheapest hours but holds back grid energy when the forecast promises sun. It never holds back more than it could still buy if the sun failed, so the car is never late. |

A charger that can switch between one and three phases can follow a small surplus on one phase. This is a charger
setting, off by default, and only some chargers can do it. See [Charging strategies](strategies.md#solar).

## The solar forecast correction

**Background, used by the battery plan.** A forecast is often off in the same way every day: shade, a wrong tilt,
an optimistic provider. SpotNav learns one factor for clear hours and one for cloudy hours, and corrects the
forecast with them. It needs at least three days. The battery plan uses the corrected forecast once it has learned.
The Hybrid strategy still uses the raw one.

Details: [Forecasts](forecasts.md#the-solar-forecast-correction).

## SpotNav's own solar forecast

**Background only.** On a site with solar production in the Energy dashboard, SpotNav also makes its own forecast.
It learns the direction and tilt of your panels and the shade at low sun from their own history, and reads an
irradiance forecast from Open-Meteo. It does not feed any plan yet. It is logged and scored each night beside the
forecast in use, so a later version can switch only if it proves better.

Only your home position, rounded to about 10 km, is sent to Open-Meteo. Details:
[Forecasts](forecasts.md#spotnavs-own-solar-forecast-background-only).

## No export at a negative price

**Shadow unless battery control is on.** When an exported kWh would cost you money, SpotNav wants the inverter to
stop exporting. It runs with the battery control switch. See
[Home batteries](home-battery.md#no-export-at-a-negative-price).

## Solar surplus for other loads

A ready-made automation runs a pool pump, a water heater or another load on solar surplus. See
[Automations](automations.md#run-on-solar-surplus).
