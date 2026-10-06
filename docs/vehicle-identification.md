# Which car is plugged in?

When two or more cars share a charger, SpotNav has to know which one was plugged in. It plans from that car's
charge level, battery size, onboard charger and target. Chargers do not report the car, so SpotNav reads what
the cars report about themselves, and asks you when that is not enough.

This runs only for a charger that more than one car can charge at. With one car nothing is identified and
nothing is asked.

## Settings

All of these are in the card's **Settings**: the section **Which car is plugged in?** (shown when more than one
car is detected) and, per car, the car's own dialog. Only an administrator can change them.

- **Vehicles at this charger.** These are the cars that can charge here. By default every car SpotNav detects
  is included. At least one must be ticked.
- **Which car is plugged in:**
  - **Automatic** (the default). SpotNav uses the cars' own reports, and asks only if they cannot decide.
  - **Always ask.** SpotNav asks at every plug-in and never switches the car by itself.
  - **Off.** The car stays the one you chose.
- **Per car: Plug sensor and Location.** These are the car's own "plugged in" sensor and its tracker, as
  SpotNav found them. If a car has more than one, you choose. You can also choose **None**, or go back to
  **Automatic**. SpotNav only reads these entities, and only for identification. They are never used as a
  charge level, a range or a way to start or stop a charge.

The **target** belongs to the car: each car has one target percent, the same at every charger. Changing it at
one charger changes it at every charger planning for that car, and when the car at a charger changes, its own
target comes with it. When two chargers change one car's target in quick succession, the later change wins
everywhere; a change of anything else at a charger (a departure, the current) never moves a target. The
**departure** belongs to the charger and stays as it is. A car with no target of its own takes, when SpotNav
starts, the highest target any charger planned it to before this feature (it is never left short), and every
charger planning for it is aligned once.

**Vehicles at this charger** also limit what the charger plans for: only those cars are offered as the target
vehicle in the card and the app, and with one car ticked that car is planned for.

## What happens at a plug-in

SpotNav starts when the charger reports that a car was plugged in. A charger that cannot tell whether a car is
connected never starts identification, so the choice stays yours.

1. SpotNav asks Home Assistant to re-read each car's plug sensor and tracker. It uses
   `homeassistant.update_entity` and never a brand's wake-up, at most once a minute per car. Waking a car
   costs its 12 V battery and part of the account's daily limit.
2. It then weighs what each car says:
   - **A car's plug sensor turned on** from five minutes before the plug-in. That car was plugged in.
   - **A car whose plug sensor went to "not plugged in"** around or after the plug-in is not here. A sensor
     that only says "not plugged in" again rules nothing out: cloud integrations write their cached value
     again on every poll (Kia's cache can say "not plugged in" for hours after the car was plugged in), and
     SpotNav's own re-read makes them write it too.
   - **A fresh position away from home** means that car is not here. "Fresh" means reported after the plug-in,
     or at most two minutes before it, and not just written again by SpotNav's own re-read. An older position may be the car's last report on its way home (many
     cloud integrations report every half hour), so it rules nothing out and SpotNav asks instead.
   - **A car already identified at another SpotNav charger** that is connected is not here. A car decided there
     counts at once, even while that charger's settings are still being saved, so two chargers never take the
     same car.
3. In **Automatic** mode, SpotNav picks a car without asking in two cases: exactly one car says it was
   plugged in, or every other car is ruled out. If two cars both say they were plugged in, SpotNav asks at
   once. Otherwise it waits three minutes and then asks. It keeps listening for 30 minutes after the
   plug-in, and if a car's own report settles it before anyone answers, SpotNav switches to that car. It
   switches automatically at most once per plug-in.
4. In **Always ask** mode, SpotNav asks at once. The cars are ordered by what they report, and SpotNav
   never switches by itself.

A car's plug sensor may report late, depending on the integration. Many cloud integrations report within
seconds to ten minutes, but some only every 30 minutes or more. If the report comes late, SpotNav asks.

## The question

The question goes to the phones chosen under **Settings → Notifications**, as the event **Which car is plugged
in?**. This event is on by default. It is a notification with one button per car. Android shows at most three
buttons, so with more cars the two likeliest get a button and a third opens SpotNav. The card shows the same
question as a banner with one button per car, and the SpotNav app will show it too.

- The **first answer wins**, from any phone, the card or the app. Any Home Assistant user may answer in the
  card; changing the identification settings stays an administrator's. Choosing the car in the card's or app's
  settings at the same time also counts as an answer. A person's answer always beats what the cars report.
- When the question is answered, or a car's report settles it, the notification on every phone is replaced
  with "Tesla chosen." or "Tesla chosen automatically." The replacement is silent: it does not alert again on
  Android, and iOS delivers it passively, without a sound or a banner.
- **No answer keeps the current car.** If the notification is swiped away on Android, the current car is
  kept too, and nothing switches it later.
- Unplugging the car for more than two minutes takes the question off the phones. A shorter unplug is looked at
  again. When nothing new is reported (a reseated cable, a connector that flaps) it is the same plug-in: what
  was decided or answered holds, and it still counts toward the one automatic switch. When the decided car
  reports, since the unplug, that it is not plugged in or away from home, or (unless a person answered)
  another car's plug sensor turns on, the cable has moved to another car: that car is identified as at a new
  plug-in, with its own one automatic switch. No question is sent while the charger is empty; a car plugged
  back in after the three minutes passed is asked about then. After twelve hours an unanswered question is
  taken off as well.
- A Home Assistant restart while a car stays plugged in is no new plug-in. What was decided at that plug-in
  is kept across the restart; a plug-in that was still undecided is identified again from the restart (the
  question follows after three minutes, as for a plug-in). A button of a question that is gone takes it off
  the phone.
- A person's answer holds for the car's stay. It is written after any automatic switch still being saved, and
  written again if another settings change got there first: it is the newest choice and is never dropped.
- The text names no number plate, place or person, because notifications pass through Google's and Apple's
  push services. It names the charger and the cars.
- Each button carries a random code that only the asked phones received, so another app cannot answer for
  you. The question counts towards the charger's limit of twelve notifications an hour. It is not held back
  by the 15-minute repeat rule, because it is asked at most once per plug-in.

## Switching the car

When the car changes, the settings are written the same way as when you change them in the card: a new
revision is stored, and the plan is calculated again from the new car's charge level and its own target.
Identification never starts, stops or takes over a charge by itself.

The charge history records how the car of each session was decided:

| `vehicle_decided_by` | Meaning |
|---|---|
| `plug_sensor` | the car's own plug sensor said so, or every other car reported that it was not plugged in |
| `location` | every other car was away from home, or was identified at another charger |
| `answered` | a person answered the question, on a phone, in the card or in the app |
| `manual` | a person chose the car in the settings (also with identification **Off**, or just before the plug-in) |
| `only_candidate` | only one car can charge here |
| `assumed` | nobody answered and nothing decided it, so the car that was already chosen was kept |

The dashboard's `identification` block and the diagnostics show what each car was judged by: its plug sensor
and position entities, the plug's state with when it changed and when it was last written, home or away (never
where), and the verdict. A field report can show from it exactly which entity and state decided. The charge
port door of a Kia (`ev_charge_port`, and its switch) is never taken for a plug: it says the port's door is
open, not that a cable is in.

## For developers

The settings fields, the dashboard's `identification` block, the vehicle row's sources and the
`identify_vehicle` / `choose_vehicle_identification` commands are described in [Apps and API](api.md).
