---
title: Get started
description: Install SpotNav, add a charger or a site, add a price source and put the cards on a dashboard.
order: 2
section: Get started
---

# Get started

This page takes you from install to a working plan. Every dialog in full detail is in
[Setup in detail](setup.md).

## What you need

- Home Assistant 2026.9 or newer.
- One of these:
  - an EV charger that Home Assistant can start and stop (see [Supported hardware](supported.md)), or
  - a home battery or a heat pump that Home Assistant can read, for a home without a charger.
- Optional: a grid meter in Home Assistant. It is needed for load balancing, solar charging and the house view.

## 1. Install

Open **Settings → Marketplace** (Home Assistant 2026.11 and newer), search for **SpotNav** and choose **Install**.
On older versions, install it from [HACS](https://hacs.xyz) and restart Home Assistant.

## 2. Add a charger

Go to **Settings → Devices & services → Add integration → SpotNav**.

1. Choose **A charger**, then **Automatic (recommended)**.
2. Pick the charger's device from the list.
3. SpotNav shows what it found. Choose **Submit**.

If the charger has its own smart charging, solar or load-balancing mode, turn it off first. Two controllers on one
charger fight each other.

No charger? Skip to step 3 and add your home.

## 3. Add your home

Your home (a *site* in SpotNav's settings) is your main fuse and grid meter. SpotNav needs it for the house card,
the home battery, the heating, solar charging and load balancing. Only a charger that just charges in the cheap
hours works without it.

1. Go to **Settings → Devices & services → SpotNav → Add entry** and choose **Your home (house, battery, heat, load balancing)**.
2. Enter your **main fuse** in amperes. SpotNav never guesses it.
3. SpotNav looks for your grid meter and lists what it found. Pick it and confirm.

Load balancing starts **off**. Turn it on in the card's settings when you are ready.

## 4. Add a price source

Your own Home Assistant fetches the spot prices. The setup offers this step by itself when your area has no source
yet:

- **Nordic and Baltic countries, and most of central Europe**: the built-in **Nord Pool** integration (one click).
- **Any other European area**: your own free ENTSO-E key. See [Prices](prices.md#where-prices-come-from).
- **Spain**: the built-in **PVPC** integration.
- **Great Britain**: the **Octopus Energy** integration, for Agile customers.

SpotNav sets the price area, VAT and energy tax from your Home Assistant's location. Check them under the card's
**Settings → Electricity price**, and add your grid fee there.

## 5. Add the cards

Open a dashboard, choose **Add card** and search for **SpotNav**. There are three cards:

| Card | Shows |
| --- | --- |
| **SpotNav** | One charger: the price graph, the plan, start and stop, and the settings. |
| **SpotNav house** | The whole house: live flows, the battery plan, earnings and prices. |
| **SpotNav heating** | The heat pump: the wanted temperature, home or away, the heat plan and hot water. |

If a card is missing from the picker after the first install, reload the browser page (Ctrl+Shift+R).

## 6. Set your first plan

On the charging card, set how much to charge (kWh or a target level) and when you leave. SpotNav plans the cheapest
hours and starts and stops the charger. That is all you need for charging.

## Next

- [What SpotNav does](what-spotnav-does.md): what each part does and what it needs.
- [Settings](settings.md): where to change things.
- [The SpotNav app](app.md): pair your phone.
