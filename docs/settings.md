---
title: Settings
description: Where SpotNav's settings live, which ones apply to the whole site, and who may change them.
order: 60
section: Settings
---

# Settings

All of SpotNav's settings are in one place: the **settings popover**. Open it from the charging card's **Settings**,
or from the gear on the house card or the heat card. The three cards share the same sections for the electricity
price, solar, the home battery and heating.

![The settings popover with a section per subject](images/card-settings.png)

SpotNav keeps settings few. Most things are found or learned, not asked for.

## Who may change what

- **Administrators** change every setting.
- **Everyone** who sees a card may start and stop charging, use **Hot water now**, and change **Display**.
- An administrator can let other users change the strategy and the plan of one charger.

## The sections

Rows marked **site-wide** apply to every charger and card on the same site.

| Section | What is there | Read more |
| --- | --- | --- |
| **Get started** | What is left to set up, one row each: the charger's and its site's (home battery, heating). **Not needed** hides a row. | [Get started](get-started.md) |
| **Electricity price** (site-wide) | Price area, VAT, energy tax, grid fee, the supplier's markup, what a sold kWh earns, the demand charge. | [Prices](prices.md), [The electricity price](price-device.md) |
| **Car** | The car's charge level sensor, battery size, consumption, onboard charger phases, charge limit. | [Target state of charge](target-soc.md) |
| **Which car is plugged in?** | Identification: automatic, always ask, or off. Optional camera. | [Which car is plugged in?](vehicle-identification.md) |
| **Charger** | Its entities, how SpotNav controls it, phase switching for solar, who may change the plan. | [The charging card](card.md#charger-entities) |
| **Site** (site-wide) | Main fuse and margin, load balancing, the priority list (which load gets energy first and gives way last), the import target, the meters. | [Site and load balancing](site-and-load-balancing.md) |
| **Solar** (site-wide) | The solar forecast sources, and **No export at negative prices** where SpotNav finds an export limit it can run. | [Solar](solar.md), [No export at negative prices](solar-export.md) |
| **Home battery** (site-wide) | The mode (its dialog explains the three and holds the wear cost), **Battery control**, its checklist and its check. Shown when SpotNav finds a battery. | [Home batteries](home-battery.md), [The battery plan](battery-plan.md) |
| **Heating** (site-wide) | Sensors and their offsets, the control path, **Control the heat**, **Hot water boost**, zones (each with its own switch), the water heater and **Control the water heater**, cooler night, setback when away. | [Heat pumps](heat-pumps.md) |
| **Pool**, **Spa**, **Sauna** (site-wide) | Each shown once that load is set up or found: its entities, powers and own control switch; the spa's temperature between baths; the pool's water sensor (second after the heat pump's own reading) and filtering per day. The heat card's phones and their events close the last of them. | [Pool, spa and sauna](pool-spa.md#the-settings) |
| **Notifications** | Which phones are told about what. | [Notifications](notifications.md) |
| **Display** | Chart time axis and price unit, for this device only. | [The charging card](card.md#display) |
| **Support** | Automations, **Beta tester**, **Hardware test**, then rows to act on: Documentation (SpotNav's site), About this card, download and send debug info, disconnect all phones, and **Support the SpotNav project** (GitHub Sponsors). The same in every card. | [Troubleshooting](troubleshooting.md), [Beta testing](beta-testing.md) |

## Settings outside the card

A few choices are made when you add a charger or a site, and can be changed later from **Settings → Devices &
services → SpotNav → Configure**. See [Setup in detail](setup.md#change-these-choices-later).
