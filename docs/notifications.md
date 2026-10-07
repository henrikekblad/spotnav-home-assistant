# Notifications

SpotNav can tell a phone when something about a charge needs attention, through the Home Assistant
Companion app (the iOS and Android app). It uses the app's own notify service, `notify.mobile_app_<phone>`,
which exists for every phone signed in to Home Assistant with the app. Nothing else has to be set up.

## Choosing phones and events

In the card: **Settings → Notifications → Change notifications**. Tick the phones that should be told
and the events they should hear about, then **Save**. Only an administrator can change it. Each charger
has its own choice, so the driver of each car can follow their own charger.

| Event | On by default | When |
|---|---|---|
| Charging stopped or did not start as planned | yes | see below |
| The charge will not be ready by the departure | yes | the departure cannot be met with the time left; once each time it becomes so |
| Charging complete | yes | the target state of charge was reached, the requested energy was delivered, the car stopped by itself when full after the plan's last window, or the plan's last window (or the hour after it that a car still charging to its own limit gets) ended while the car was charging |
| Charging started | no | the charger starts charging, for whatever reason |
| Car plugged in / Car unplugged | no | for a charger that can say whether a car is connected |
| New plan | no | a plan different from the one before is installed, with its start, energy and estimated cost |
| Which car is plugged in? | yes | at a charger more than one car can charge at, when SpotNav cannot tell which car was plugged in: a question with one button per car ([vehicle identification](vehicle-identification.md)) |

A new plan that SpotNav calculates within a minute after someone changed that charger's settings (in the app, the card or a SpotNav entity) is not told, nor does it wake the app, because the person already sees it; problems are never held back this way.

With no phone ticked nothing is sent. A phone that is later removed from Home Assistant stays in the
list, marked as not found, and is skipped.

## "Charging stopped or did not start as planned"

This is told only for a charge SpotNav expected: a window of the installed plan is open now. It is told
when, for three minutes, the charger:

- did not start the window's charge (the start failed, or the charger did not take it);
- stopped it, by something other than SpotNav (the charger's own app, a button on the charger, a fault);
- is unavailable in Home Assistant;
- is held by its own schedule or load balancer, or has its own enable switch off;
- charges, but the car takes no current.

It is **not** told for a window's planned end, a target or energy that was reached, a person's Start or
Stop in the card, the app or a button (it pauses Auto for the plug-in), a paused Auto, solar or hybrid running the charger, load balancing
pausing the charge for want of headroom (the card's status says that), or a car that was unplugged. Nor
is "did not start" told while a charger that can say whether a car is there says neither (a fault, an
offline charger, or a Wallbox showing `Ready`, which the Wallbox integration reports with or without a
car), and a car that takes no current because it sits at its own charge limit, below the plan's target,
is not a fault.
It is told once while it lasts; if the charge recovers and stops again, it is told again.

The same event also tells when SpotNav gives up stopping a charger under a person's **Stop**: a charger
that keeps charging, or begins again, after three stops within ten minutes is left alone, and the
phones hear "The charger keeps charging although it was stopped". The card's status says so until the
person presses Start, Stop or Resume, or the car is unplugged. This one is not held back by a "did not
start" told shortly before it.

## What a notification says

A short line in Home Assistant's language (English, Swedish, Danish, Norwegian, Finnish, German, Dutch, French or Spanish), titled
with the charger's name, for example:

> **SpotNav · Garage**
> Laddningen är klar: målet 80 % är nått. 12,4 kWh laddat, 18,30 kr.

Charging complete gives the energy and the cost of that charge from its session, a new plan its start
time, planned energy and estimated cost. Tapping the notification opens Home Assistant on the dashboard
the settings were saved from.

The same event for the same charger is not sent again within 15 minutes, and a charger sends at most
twelve notifications an hour. Each kind has its own tag, so a phone replaces an older notification of
the same kind instead of stacking them. On Android they arrive in a notification channel named
**SpotNav**, which can be given its own sound or importance in the phone's settings.

## Instant notifications in the SpotNav app

The SpotNav Android app can tell its own phone about the same events without the Companion app. It
checks Home Assistant every 15 minutes by itself; with **Instant notifications** turned on in the app,
Home Assistant also wakes it at once through the SpotNav server when one of the events the app chose
happens. The wake-up carries nothing but an opaque reference to the phone: the app then reads what
happened from Home Assistant, as it always does. The Home Assistant diagnostics of a charger say
whether an app is registered and how the last wake-up went.

**Testing.** The action `spotnav.send_test_notification` (Developer tools → Actions), optionally for
one charger, sends the app a test push and the Companion phones chosen above a test message. It is
refused when no charger has the app's instant notifications turned on, and the result per charger is
logged.

## For the paired app and automations

The choice is the settings record's `notifications` field (see [Apps and API](api.md)). The
[Charger events](api.md#home-assistant-events) entity fires for automations whatever is chosen here.
