---
title: "The year with SpotNav"
description: "A short story of your year in figures: what SpotNav saved, the car, the sun, the battery and the most expensive hour. When it appears, where the figures come from and how to show it again."
order: 83
section: Help
---

# The year with SpotNav

In the Swedish card it is called "Året med SpotNav".

## In short

- **What it is.** A few full-screen pages that tell your year in figures: what SpotNav saved, how much of the car's
  charging was in cheap hours, how much the sun gave, what the battery earned or saved, and the most expensive hour of
  the year. A page appears only where your home has something to tell.
- **When.** From 10 December to 31 January, as a banner at the top of the house card and the charger card and as a
  row under **Settings → Support**. Once you have opened the story or closed the banner, the banner never returns. The
  row stays until 31 January.
- **What it needs.** Nothing to set up. SpotNav works it out each night from what it already keeps and from Home
  Assistant's statistics. Nothing leaves Home Assistant unless you share the picture yourself.

## The pages

Pages without anything to stand on are left out. The first and the last are always there.

| Page | What it says | Left out when |
| --- | --- | --- |
| Your year | The cover. Says "Part of the year" with a date when SpotNav's own record starts after 7 January. | Never |
| Saved | What SpotNav saved over the year, split into the battery, the car and the heating, each from the day it started counting. | The total is under one krona (or euro, pound) or fewer than seven days have anything counted |
| The car | The share of the car's charging that fell in the day's cheapest third of hours, the number of charges, the kWh and the cheapest charge. | No charges, or too little of the charging has a price |
| The sun | The kWh the panels gave and the share used at home. | No solar meter in the Energy dashboard. The share is left out without an export meter |
| The battery | What it earned and the average price it bought and used at, its cycles, or what it saved. The kWh in and out. | No battery meter. Money needs 14 days of counted days |
| The fuse | How many times load balancing lowered a charge for a while instead of letting the fuse blow. | Under three times, or under 28 days counted |
| The most expensive hour | Its price, the day and hour, and what you bought from the grid in it. | Fewer than 28 days with prices |
| Thank you | The only place the story asks for support, once, with a link to GitHub Sponsors, and a button that saves a picture to share. | Never |

In December the story covers 1 January up to yesterday. In January it is last year, whole.

## Where the figures come from

- **Energy and charges.** The sun, the battery and the grid come from the meters in Home Assistant's Energy dashboard
  and their long-term statistics, which reach back as far as the sensors have kept them. The car's charges come from
  SpotNav's own charge sessions, and from Home Assistant's statistics for older ones.
- **Prices are spot plus your fees.** Every price on the pages is the spot price with the fees you have set, as they
  are now. Prices are kept per day: a relay file or an ENTSO-E fetch reaches up to a year back, Nord Pool about 55
  days, and some price sources only from your install.
- **What SpotNav saved** is only for time SpotNav controlled. The battery and heating counts start with SpotNav 3.0.
  The car is exact for charges started with the plug-in time recorded (3.0 and later). Earlier charges that SpotNav
  planned are estimated against each day's average price.
- **What the battery saved** is worked out at each hour's price, whoever controlled the battery, so a battery run by
  its own optimiser counts too.
- **The fuse** counts from the beta that introduced the page, so most homes will not have it for 2026.

## What ≈ and "since" mean

- **≈** before a figure means it is estimated: a charge read from the hourly statistics is counted by the hour, a
  cycle count is worked out from the energy, and charges before 3.0 are estimated against each day's average price. The
  mark stays on the picture you share.
- **Since {date}** means the figure covers less than the whole year. A recent install has only its own days, and the
  price history can be shorter than the year (see above). SpotNav says so rather than showing a short span as a year.

## Showing it again, or early

The banner and the row follow the date. To see the story any time (a new installation, or to see it again), an
administrator runs the action **SpotNav: Show the year's review** (`spotnav.show_year_review`) under **Developer
tools → Actions**. It works the review out now, offers it in the cards for seven days whatever the date, and answers
with the figures, so they can be read there too.

| Field | What it does |
| --- | --- |
| Site | Which site. Leave it out when there is only one. |
| Year | The year to review. By default this year in December, last year in January, else the year so far. |
| Show the banner again | On by default: shows the banner again to everyone who opened or closed it. |

## Who has seen it

- **In Home Assistant** it is remembered per user. Opening the story or closing the banner for your user hides the
  banner for you only, in every browser and in the companion app's view of the card.
- **In the SpotNav app** it is remembered per phone.

## The picture to share

On the last page, **Save a picture to share** makes a picture with a few of the figures: only kronor, kWh and
percentages, never names, address or the time of the expensive hour. It is made in your browser and goes nowhere
unless you share it. Where sharing a file is not possible it is saved as a file, or shown so you can touch and hold it
to save.

The story's last page is the only place SpotNav asks for support, and only once. The banner never repeats it.

See also [Settings](settings.md), [The house card](house-card.md) and [Prices](prices.md).
