# How the forecasts are tested

SpotNav 3.0 plans the home battery, the cars and the heat from forecasts. This page explains how the house load
forecast was tested, what we tried, what we threw away and why. The model as it works today is described in
[The house load forecast](forecasts.md#the-house-load-forecast); this page does not repeat it.

All numbers come from one house: the developer's own house in southern Sweden (heat pump, home battery, EV).
Read them as one data point, not as a promise (see [Caveats](#caveats)).

## Why the forecast matters, and how much

A plan is only as good as what it expects. If the plan expects the house to draw 1 kWh less than it does during one
of the day's dearest hours, the battery has too little left for that hour and the house buys the difference at the
top price. The error is real, but it is small money. The question is how small, and whether a better forecast is
worth its extra code.

We measured that. On the developer's house, over 89 winter days, a **perfect** house forecast (the real load,
known in advance) would have earned only **12 kr** more than today's model. So there is very little money in the
house forecast on this house. That is the headline of this page.

## Today's house model, in plain words

For each hour of the coming two days, SpotNav looks at the same hour on similar days: weekdays with weekdays,
weekends (and holidays) with weekends. It reads the last six weeks, and newer days count more: a day two weeks ago
counts half as much as yesterday. Then it takes the typical value of those hours. The battery plan uses the
median (half of the days were lower, half higher). The cars use a figure with some room: three days in four were
lower.

Example: tomorrow is a Tuesday and the hour is 18:00. SpotNav looks at 18:00 on recent weekdays, gives last
week's more weight than the week before, and takes the middle value, say 1.4 kW. If tomorrow is colder than those
days were, and the house really is electrically heated, it adds a little for each degree. If today so far runs
higher than usual, it nudges the rest of today.

## How we test

**Forecast error.** The simplest measure is the *mean absolute error* (MAE): for every hour, how many kW the
forecast was off, ignoring the direction, averaged. An MAE of 0.35 kW means the forecast is on average 0.35 kW from
what happened. SpotNav also gives a band around the figure, from the 10th to the 90th percentile; the band is
good when about 10 % of hours fall below it and 10 % above it.

**Money.** A small error is no use if it does not change what the plan does. So we also rerun the battery planner on
frozen real days, with each candidate forecast, and count what the day would have cost:

* 76 autumn days (1 September to 15 November) and 89 winter days (December to February).
* The planner runs as it would live. The forecast is *issued at 13:00 for tomorrow*, when the next day's prices
  are published, using only what was known at that moment. It never sees the future. The weather used is the
  forecast that was available then, not the weather that came.
* The result is compared with three references: **no battery**, **the battery's own optimiser** (Sigenergy AI,
  which really ran the battery those days) and **the perfect plan in hindsight** (the best the battery could have
  done if every hour had been known).

Because the differences between candidates are a few kronor, we also resample the days week by week to see whether
a result holds up or is luck.

## Results

Forecast error, next day as issued at 13:00, December to February (lower is better), and money on the winter days
against today's model, in kronor over 89 days (Max vinst mode):

| Forecast | MAE, kW | Money vs today's |
| --- | ---: | ---: |
| Same hour last week | 0.600 | not run |
| Mean of the last four same weekdays | 0.540 | not run |
| **Today's model** | **0.358** | **0** |
| Degree-hour model (A) | 0.350 | -1 |
| Heat-aware model (B) | 0.344 | -3 |
| Analog days (C) | 0.390 | -3 |
| Today's and B, half each (D2) | 0.337 | 0 |
| All four blended (D4) | 0.339 | -1 |

What the table says:

* The best candidate (D2) cuts the error by about 6 % in winter. In kronor it is worth nothing: every candidate
  is within 3 kr of today's model over 89 days.
* Against the battery's own optimiser, today's model is about level in winter: 1 672 kr against 1 663 kr, and
  we treat that as within plus or minus 20 kr, the error of the data itself. It is not a win.
* In autumn the battery's own optimiser saved 94 % of what the perfect plan could (4 080 of 4 350 kr over 76
  days). SpotNav, planning with today's forecasts, reached 91 % (3 960 kr), 125 kr less than the optimiser.
  Most of that gap lies elsewhere than in the house forecast: in the sun forecast and in the planner's own rules.

## Tried and rejected

* **Same hour last week.** Simple, but one week is noisy: the error is 0.600 kW against 0.358 kW.
* **Mean of the last four same weekdays.** Better, still far behind at 0.540 kW. A single odd day moves it too much.
* **Degree-hour model (A).** Adds a heating term from the outdoor temperature to the weekday profile. A slightly
  smaller error, no money (-1 kr), and a second model to keep running.
* **Analog days (C).** Find the past days that looked most like tomorrow and copy their load. The weakest single
  model (0.390 kW): after a change in habits or a cold snap there are few days to copy.
* **Four models blended (D4).** Not better than blending only today's model and B (D2): the winter pinball loss
  differs by 0.4 % and the error is slightly worse. Two more models for nothing.
* **Weights learned from recent errors.** Letting the blend give more weight to whichever model did best lately was
  no better than a plain 50/50 (0.11380 against 0.11387).
* **A fading correction from the last hours.** It helps for the next few hours and does nothing for tomorrow,
  which is what the battery plan needs.
* **Planning on the 60th or 70th percentile instead of the median.** It lowers the error over the dearest hours,
  but in the money it is not a robust win: +3 kr in winter, -2 kr or worse in autumn. Today's median is kept.
* **A median bias correction as the default.** It helps a house whose temperature history and forecast come from
  different sources, and it is slightly worse where they come from the same one. Not on by default.
* **The first round's "+9 kr".** An early result said one candidate earned 9 kr more than the battery's optimiser.
  It was withdrawn: the car's charging had been counted as house load in part of the data, and one lucky day
  did the rest. We keep the withdrawal here on purpose. With the car taken out properly, no candidate changes the
  money.

## What we kept, and what comes next

* **Logging the temperatures.** SpotNav keeps the outdoor temperature history for about 400 days and logs the
  temperature forecast it used for each hour. A past forecast cannot be fetched later, so without the log a
  future test is impossible.
* **Same-source temperature.** If the history comes from a heat pump's own outdoor sensor and the forecast from a
  weather service, they disagree by a degree or two. Moving the forecast by the last 14 days' difference removed
  the error that the mismatch caused in the test (0.402 to 0.363 kW). That test used a garage sensor standing in
  for a heat-pump sensor, so the gain on a real heat pump is expected, not proven.
* **The heat-aware model (B)** runs in the background only. It feeds no plan. It is promoted only if a season of
  logs, or another house, shows that it earns money.

## The solar forecast

An experiment comparing our own solar forecast with Solcast is running. This section will be filled in when it is
done.

## Caveats

* One house and one winter (plus an autumn). The battery, the prices and the heating are those of one household.
* The house heats with a heat pump. On a house with direct electric heating, the heat is a larger share of the load,
  and a heat-aware forecast may pay where it did not here. That is what the background log is for.
* The comparison with the battery's own optimiser is within plus or minus 20 kr in winter. Do not read a few
  kronor of difference as a win or a loss.
