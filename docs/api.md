# Apps and API

For developers of apps and Home Assistant clients, and anyone who wants to see what SpotNav exposes. Everyday users do not need this page.

Everything the card and the SpotNav app do goes through two transports that share one set of
contracts: authenticated **WebSocket commands** (for the card and other Home Assistant clients)
and a per-charger **webhook** (for clients that cannot open a WebSocket, such as the app). All
contracts are version 1; each response carries `api_version`. A request for another version is
refused with `spotnav_unsupported_api_version`.

**Version 1 is frozen (since SpotNav 3.0).** Every document on this page, over both transports, changes under
`api_version` 1 only by addition: a new key, a new action or command, a new stable code, a new value where the
document says the list is open (status codes, capability flags). A key is never renamed, removed or given another
type or unit, and a required request field is never added, under version 1: such a change is a new `api_version`
for that contract, answered beside version 1 for as long as a released app or card asks for it. So a client
decodes the keys it needs strictly and ignores the keys it does not know. The exceptions are the documents an app
released before 3.0 decodes with exact keys (the dashboard, the settings record, the sessions and the vehicle
rows): the webhook adds to those only for a request that names the addition in `reads` (below). The contract
fixtures under `tests/fixtures/` are the shapes, written by the backend and read by the card's and the app's tests.

**Money.** A document that states sums of money carries one block for its units,
`money: {"currency", "major_unit", "minor_unit"}` (`"SEK"`, `"kr"`, `"öre"`), `null` where the site has no
electricity price. The house and the heat documents share it (the heat documents' `major`/`minor` of the 3.0
previews are gone). Sums are in the minor unit unless a key says otherwise.

## Pairing

The app never holds a Home Assistant password or token. It pairs through one fixed, public
webhook, `POST /api/webhook/spotnav_pairing`, that grants nothing by itself:

1. `{"version": 1, "action": "request", "code": "123456", "device": "Pixel"}` records a pending
   request and shows an approval item in Home Assistant with the device name and the code.
   The code must be six digits; the answer is `{"ok": true, "request_id": "..."}`.
2. The person compares the code and chooses **Approve** or **Deny** in Home Assistant.
3. `{"version": 1, "action": "poll", "request_id": "..."}` answers `pending`, `denied`,
   `expired` or `approved`. `approved` carries `url` (the instance address) and `chargers`,
   a list of `{id, name, webhook}`. It is delivered once.

Requests live in memory for five minutes, at most eight at a time, and codes and ids are never logged.
The chargers' webhook ids are the credentials handed over; each one is scoped to its own charger.
The approved handshake is the only way a webhook id leaves Home Assistant: no entity carries one in
its state or attributes. (The *App connection* and *App pairing* sensors of releases up to 1.17.0 did;
they are removed on upgrade, together with their recorded history.) Treat webhook ids as secrets.

## Charger webhook

`POST /api/webhook/<webhook id>` with a JSON body `{"version": 1, "action": ..., ...}`. The
webhook is bound to its own charger. A `charger_id` in the body is ignored. Success is
`{"ok": true, "action": ...}`; errors are `{"ok": false, "error": ...}` with HTTP 400 (bad
request), 409 (refused by the automatic-execution boundary, with a stable code), 429 (per-vehicle rate
limit, or the plan preview's, with `Retry-After`) or 502 (the charger command failed). A `start` or an immediate `stop` the
charger executed whose pause could not be saved answers `{"ok": true, "action": ..., "warning":
"reconcile_failed"}`: the pause holds in memory and the save is retried.

| Action | Effect |
| ------ | ------ |
| `dashboard` | The dashboard document below, for this charger. Takes an optional `api_version`. |
| `sessions` | The charge history, the same request and answer as `spotnav/get_sessions` below (`month`, `format`, `limit`, `from`, `to`), with `"ok": true` and the `action` beside it. With `"reads": ["plug_in_saving"]` each summary and session also has `plug_in_saving` (see [the site over the webhook](#the-site-over-the-webhook)). A refusal is HTTP 400 with `{"ok": false, "error": "spotnav_invalid_range" \| "spotnav_unsupported_api_version", "action": "sessions"}`. Nothing is withheld. |
| `price_days` | The charger's electricity prices for today and tomorrow and the history profile, as day documents (see below). Takes an optional `api_version` (`1`). |
| `settings` | Replace the charger's settings: `{"expected_revision": n, "settings": {...}}`. |
| `plan_preview` | What a `settings` replacement of this body would plan, with nothing saved: `{"settings": {...}}` (see **Plan preview over the webhook** below). |
| `start` | Start charging now; optional `amps`. Pauses automatic execution for the plug-in session (pause choice `manual`). |
| `stop` | Stop now, which pauses automatic execution for the plug-in session (pause choice `manual`), or with a `choice` (`next_period`, `until_tomorrow`, `until_resumed`) pause it for that span instead. |
| `resume` | Clear a pause. |
| `refresh_vehicle` | Re-read the vehicle's Home Assistant entities (`vehicle_id`); never a brand's wake-up (Kia Uvo may still ask the car itself, see [target SoC](target-soc.md)). |
| `set_charge_limit` | Write the vehicle's charge-limit entity (`vehicle_id`, `percent`). |
| `update_vehicle` | Change a vehicle's capacity, consumption, onboard charger, target, minimum charge level or wake for a reading, with `expected` values. |
| `update_site_settings` | Change car or home battery first (`solar_priority`), forecast sources or the import target of the charger's site (see below). |
| `update_charger_priority` | Change this charger's priority on its site: `{"priority", "expected"}` (see below). |
| `update_phase_switching` | Turn phase switching for solar on or off for this charger: `{"enabled", "expected"}` (see below). |
| `choose_price_source` | The electricity price this charger uses: `{"price_entry_id"}` (see below). |
| `answer_price_duplicate` | Answer the same-area question: `{"area_id", "keep_entry_id"}` (see below). |
| `update_grid_tariff` | The electricity price's grid tariff (a percentage of spot, a high-load fee and its window): `{"expected_write_revision", "grid_tariff"}` (see below). |
| `push_register` | The app's instant notifications: `{"push_ref", "events"}`, or `{"push_ref": null}` to stop (see below). |
| `identify_vehicle` | Answer "which car is plugged in?": `{"vehicle_id"}` (see [vehicle identification](#vehicle-identification)). |
| `choose_vehicle_identification` | Choose a car's plug or location source: `{"vehicle_id", "source", "entity_id"}` (see [vehicle identification](#vehicle-identification)). |
| `update_guest` | The guest amount a guest car's plan starts from: `{"amount_kwh"}` (see [the guest car](#the-guest-car)). |
| `dismiss_setup_item` | Answer a "Get started" row with "Not needed": `{"item"}` (see below). |
| `fix_own_mode` | Set one of the charger's own settings back to the value SpotNav works with: `{"entity_id", "value"}` (see below). |
| `release_import_target` | Set the site's import target aside for this charge until its departure: `{}` (see below). |
| `get_power_month` | One month of this charger's site's measured import: `{"month"}` (optional), the answer of `spotnav/get_power_month` with `"ok": true` and the `action` beside it; a refusal is HTTP 400 with `{"ok": false, "error", "action"}` (see [Power by month](#power-by-month)). |
| `suggest_import_target` | A suggested import target for this charger's site from Home Assistant's statistics: `{}`, the answer of `spotnav/suggest_import_target` with `"ok": true` and the `action` beside it; a refusal is HTTP 400 with `{"ok": false, "error", "action"}` (see [A suggested import target](#a-suggested-import-target)). |
| `get_house` | The house card's view of this charger's site: `{"date"}` (optional), the answer of `spotnav/get_house` (see [the site over the webhook](#the-site-over-the-webhook)). |
| `get_house_now` | The site's live flows: `{}`, the answer of `spotnav/get_house_now`. Cheap enough to poll every 10 s. |
| `get_house_history` | One day, month or year of the house: `{"period", "date"}`, the answer of `spotnav/get_house_history`. |
| `get_house_loads` | The pie behind House or Heat: `{"period", "date", "kind"}`, the answer of `spotnav/get_house_loads`. |
| `get_heat` | The heat card's view of the site: `{}`, the answer of `spotnav/get_heat` as someone who is no administrator reads it. |
| `get_heat_history` | One day, month or year of the heating: `{"period", "date"}`, the answer of `spotnav/get_heat_history`. |
| `update_heat` | The dial, home or away, the away setback, the cooler night and each zone's and indoor sensor's offset: `{"changes": {"target_c", "mode", "away_until", "away_delta_c", "night", "zones", "indoor_offsets"}, "expected"}`, the envelope of `spotnav/update_heat`. Nothing else may be named. |
| `battery_control` | The site's battery control status; with `{"changes": {"battery_mode"}}` the battery mode is written first. Nothing else may be named. |
| `get_site_settings_view` | The settings' site block: `{}`, the answer of `spotnav/get_site_settings_view`. |

**Charge periods.** `max_periods` is `null` for automatic periods (the default: the planner counts a start
cost of a quarter of a kWh at the window's average price for each period and keeps each period at least half an
hour; see [strategies](strategies.md#cheapest)), or a whole number from 1 to 8, the most periods a plan may use.
Anything else is refused with `invalid_periods`. A record stored before automatic periods existed is read as
automatic once, and saved that way at its next write; a number set after that stays. The released app refuses
`null` here, so the webhook answers `8` in its place unless the request asks for it (`"reads": ["auto_periods"]`),
and a webhook replacement without that `reads` that says `8` while the stored value is automatic keeps automatic
(any other number is stored). The select entity **Charge periods** (`auto`, `1` to `8`) replaces the earlier
number entity, which is removed.

**Withheld settings fields.** The settings record has a `departure_weekdays` (an optional list of
weekday numbers, 1 Monday to 7 Sunday, at least one, default all seven: the days a daily departure
applies on) and a `departure_date` (an optional `YYYY-MM-DD`,
or `null`, for a departure on a particular day). The webhook leaves it out of every `settings`
record it answers: the dashboard's, and the `settings` action's success and failure alike. The
released Android app refuses a settings record with a field it does not know, and with it the
whole dashboard, so the field stays withheld until an app that reads it is out. The WebSocket
carries it. A `settings` replacement over the webhook may leave either out, and then the stored value
is kept; one that names it is accepted and applied, but the answer still does not show it. The
withheld fields are listed in `APP_UNREAD_SETTINGS` in `custom_components/spotnav/api/webhook.py`.

The record also carries a read-only `fiscal_included`: the fiscal components (`vat`, `tax`, `transfer`)
the selected area's published price already contains. For a Great Britain region (Octopus Agile) that
is all three: they are locked as included in the price and nothing is added for them, whatever the
area's override says. It is withheld from the webhook like the two fields above (ask with
`"reads": ["fiscal_included"]`); a replacement that echoes it is accepted and it is never stored.

The record also carries `notifications` (see [Notifications](notifications.md)): `targets` (notify
service names such as `mobile_app_pixel_8`, at most ten), `events` (any of `plan_stopped`,
`plan_at_risk`, `charge_complete`, `charge_started`, `plugged_in`, `unplugged`, `plan_installed`,
`vehicle_identify`; default `plan_stopped`, `plan_at_risk`, `charge_complete` and `vehicle_identify`; a choice
stored before `vehicle_identify` existed has it on), `url` (a Home Assistant path a tap opens, or `null` for the default
dashboard) and a read-only `available` (`[{"service", "name"}]`, the Companion app's notify services
that exist now, named after their phones). A replacement may leave the field out, and then the stored
choice is kept; `available` may be echoed and is ignored; a bad value is refused with
`invalid_notifications`. It is withheld from the webhook like the fields above (ask with
`"reads": ["notifications"]`).

The record also carries `fill_to_limit` (a boolean, default `false`): "Fill", the kWh slider's last step.
While it is `true` and the driver is `manual_kwh`, the need is the battery's room (`soc.room_kwh`, to the
car's own charge limit) at every calculation, so it follows the car between plug-ins, and the car ends the
charge when it is full; `requested_kwh` is kept beside it and is what is planned while no room is known
(no level or no battery size). A target ignores it. A replacement may leave it out, and then the stored
choice is kept, unless the same replacement changes `requested_kwh`: a client that does not know the field
chose an amount, and that clears it. Setting the requested-energy number entity clears it too. A value that
is not a boolean is refused with `invalid_energy`. It is withheld from the webhook like the fields above
(ask with `"reads": ["fill_to_limit"]`), so an older app neither sees it nor is offered a record it would
refuse.

The record also carries two fields for [vehicle identification](vehicle-identification.md): `vehicle_ids`
(the vehicles that can charge at this charger: `null` for every detected vehicle, the default, or a non-empty list
of different vehicle ids) and `identify_mode` (`"automatic"`, the default, `"ask"` or `"off"`). `vehicle_ids` also
limits what the charger plans for: the dashboard's `vehicles`, `target_vehicle_id`, the `soc` block's choices and
the planner's car only name those vehicles (the only one of them is the target when none is chosen). The
additive root `vehicle_choices` (`[{"id", "name"}]`, by name) lists every detected car whether or not it is one of
this charger's: the list a client ticks `vehicle_ids` from, so a car left out can be ticked again (every car
ticked is `null`). A
replacement may leave either out, and then the stored value is kept; a bad list or mode is refused with
`invalid_vehicles`. Both are withheld from the webhook like the fields above (ask with
`"reads": ["vehicle_ids", "identify_mode"]`), so an older app never sees them, and its replacements keep them.

The record also carries `identify_camera`, the charger's [camera for identification](vehicle-identification.md#the-camera):
`null` (the default: no camera) or `{"camera_entity_id": "camera.…", "ai_task_entity_id": "ai_task.…" | null, "frame":
{"x", "y", "w", "h"} | null}`. `ai_task_entity_id` `null` is Home Assistant's default AI Task entity; `frame` is the
parking spot as fractions of the picture (its top left corner and its size, each 0-1, at least 0.05 each way,
inside the picture; four decimals are kept), `null` for the whole picture. A replacement may leave it out, and
then it is kept; anything else is refused with `invalid_camera`. It is withheld from the webhook like the fields
above (ask with `"reads": ["identify_camera"]`).

The record also carries `users_change_plan` (a boolean, default `false`): whether Home Assistant users who are
not administrators may change this charger's strategy and plan over the WebSocket. Only an administrator changes
it. A replacement may leave it out, and then the stored value is kept; a value that is not a boolean is refused
with `invalid_permission`. It is withheld from the webhook like the fields above (ask with
`"reads": ["users_change_plan"]`).

Since 2.3 the record carries `if_not_ready` (`ask`, the default, `release` or `hold`): what the site's import target
does when the plan cannot be ready by its departure under it (see
[Holding the chargers under the import target](site-and-load-balancing.md#holding-the-chargers-under-the-import-target)).
A plan field: a user who may change the plan may change it. A replacement may leave it out, and then the stored
value is kept; any other value is refused with `invalid_departure`. It is withheld from the webhook like the fields
above (ask with `"reads": ["if_not_ready"]`), so an older app's full write keeps it.

Since 3.0 the record carries `departure_calendar`: `null` (the default) or a Home Assistant calendar entity
(`calendar.…`, any calendar integration) whose events are departures. The first event of a day replaces the daily
departure on its day (its start is the departure; later events that day are not departures, also once the first has
gone; a day without one keeps the daily departure on its weekdays); an all-day event is a departure at the daily
time on its first day, and that day's first unless a timed event that day starts earlier (at the same minute the
timed one is the first); a multi-day event is one at its start. A target in the
event's title or description (`mål: 80`, `target: 80 %`, `80%`, `30 kWh`) is the plan's for that departure where it
fits the driver (a level for a target, energy for an amount). A one-off `departure_date` still ahead wins over the
calendar; the departure switch off or a guest car reads no calendar; a calendar that cannot be read leaves the
daily departure (events are read every 15 minutes, and a calendar that stops answering keeps its last answer for
an hour). Only events within `MAX_DEPARTURE_DAYS_AHEAD` (7) days count. A plan field. A replacement may leave it
out, and then it is kept; anything that is not `null` or a calendar entity id is refused with
`invalid_departure`. It is withheld from the webhook like the fields above, and so is the dashboard's
`departure_calendar` block (ask with `"reads": ["departure_calendar"]`).

Since 3.0 the record also carries `departure_calendar_only` (a boolean, `false` by default; stored only when `true`):
the calendar alone decides. A day without an event then has no departure, and the plan runs to the next event; with
no event within the seven days there is no departure at all, and the charger plans as with the departure switched
off (the block's `state` is `no_event`). A one-off `departure_date` no longer goes first: with the switch on and a
calendar chosen every write clears it (the card, the settings API, the date entity and an app's full write alike), and
one stored earlier is ignored in planning. A calendar that cannot be
read falls back to the daily departure as without the switch (`unavailable`): a car that must leave is safer
charged by the daily time than not at all. A plan field. A replacement may leave it out, and then it is kept; a
value that is not a boolean is refused with `invalid_departure`. It is withheld from the webhook like the fields
above (ask with `"reads": ["departure_calendar_only"]`).

The additive root block `departure_calendar` is present where Home Assistant has a calendar or one is chosen:
`{"calendars": [{"entity_id", "name"}], "entity_id", "state", "departure", "summary", "all_day", "target_percent",
"target_kwh", "target_used"}`, every key always present. `state` is what the last calculation made of the chosen
calendar: `used` (its departure is the plan's: `departure` the instant, `summary` the event's title or `null` when
an event only moved the daily departure to its next day, `target_percent`/`target_kwh` what its text gives and
`target_used` `percent`, `kwh` or `null`), `daily` (no event moves the daily departure), `date` (a chosen date
wins), `unavailable` (it cannot be read: the daily departure is used), `no_event` (the calendar alone decides and
has no event ahead: no departure), `off` (no departure, or a guest car), or `null` (none chosen, or not calculated
yet).

The additive root block `sun` is present where the site has a solar forecast (ask with `"reads": ["sun"]`):
`{"hours": [{"start", "kw", "measured"}], "quarters": [{"start", "kw"}], "peak_kw"}`, the sun's mean power per local hour of today and tomorrow in kW. Before the
running hour `kw` is the measured production (`measured` true), from the running hour on the corrected forecast the plan
uses (`measured` false), the running hour included; an hour with no figure is left out. The forecast reaches 24 hours
ahead, so tomorrow is partly there. `quarters` is today's measured mean power in kW per quarter hour up to now (at most
96, from the recorder's 5-minute statistics, read off the event loop and refreshed every five minutes); it is empty where
those are missing, and a client then draws the hourly measured figures. A client that has quarters draws them in place of
the measured hours they cover and starts the forecast where the last quarter ends. `peak_kw` is the site's peak
production in kW, for a client to scale the sun against so a dull day is drawn small: the highest hour ever measured
(the solar correction's record), else the array's size where the forecast integration states it; the key is left out
where neither is known, and the client then scales against the day's own tallest hour. A client uses the larger of
`peak_kw` and the day's tallest hour, so a record hour still fits.

**Who may replace the settings.** An administrator may change any field. A user who is not one is refused with
`spotnav_not_admin` while the charger's `users_change_plan` is off; while it is on, a replacement from them is
accepted only when every field it changes is one of the strategy and plan fields: `strategy`, `requested_kwh`,
`fill_to_limit`, `driver`, `target`, `departure_enabled`, `departure`, `departure_date`,
`departure_weekdays`, `departure_calendar`, `departure_calendar_only`, `amps` and `if_not_ready` (`NON_ADMIN_FIELDS` in `custom_components/spotnav/api/settings.py`). One that
also changes anything else (`area_id`, `overrides`, `max_periods`, `notifications`, `vehicle_ids`,
`identify_mode`, `identify_camera`, `users_change_plan`) is refused whole with `spotnav_not_admin` and
nothing is written. The check is made again inside the store's lock against the record being replaced.

**A car's target is the car's, at every charger.** The target percent is a vehicle property, beside its battery
size: a vehicle row in `vehicles` carries the additive `target_percent` (0-100, `null` when never set), and
`update_vehicle` writes it (`changes: {"target_percent": 85}`, `null` clears; anything else is `invalid_target`;
`expected` may name it). The settings record's `target.target_percent` is its planned car's: a replacement that
sets it sets the car's, and every other charger planning for that car takes it by a settings write of its own
(its revision moves). A replacement that changes `target.vehicle_id` but leaves `target.target_percent` as it was
gets the new car's target, when it has one. Only a write that changes the target moves the car's; when two
chargers change it in quick succession the later change wins at every charger. At set-up, a car with no target of
its own takes the highest target any charger planned it to before this release, and every charger planning for it
is aligned once. Nothing is withheld for it: the released app reads a vehicle row
key by key and ignores keys it does not know (as it does the dashboard's root blocks).

**A car's minimum charge level.** A vehicle row carries the additive `min_percent` (10-80 in steps of 5, `null`
when off), the car's [minimum charge level](target-soc.md#minimum-charge-level), and `update_vehicle` writes it
(`changes: {"min_percent": 30}`, `null` turns it off; anything else is `invalid_min_percent`; `expected` may name
it). While the car's known level is below it SpotNav charges at once and the status headline is
`min_soc_charging` (`percent`: the floor that applies, capped at the target and the car's own limit; tone `normal`).
A released app words a code it does not know as "see Home Assistant", so the webhook leaves `min_percent` out of
every vehicle row (the dashboard's `vehicles` and `update_vehicle`'s `vehicle`) and says `charging_now` (`until`
`null`) in place of `min_soc_charging` unless the request asks for them (`"reads": ["min_soc"]`). An app offers the
setting only for a row that carries `min_percent`.

**Waking a car for a reading.** A vehicle row carries the additive `wake_for_reading`: `true` or `false` for a car
whose integration has a wake SpotNav knows, `null` when it has none (the setting is not offered). `update_vehicle`
writes it (`changes: {"wake_for_reading": true}`; `false` or `null` turns it off; a value that is not a boolean is
`invalid_wake_for_reading`, `true` for a car with no wake is `wake_unsupported`; `expected` may name it). See
[waking the car](target-soc.md#waking-the-car-for-a-reading). The webhook leaves `wake_for_reading` out of every
vehicle row unless the request asks for it (`"reads": ["wake_for_reading"]`); an app offers the setting only for a
row where it is `true` or `false`.

**A charge load balancing paused.** Inside an installed period, while load balancing holds the plan's charge back
(no room for the car's minimum current), the status headline is `balancing_paused` (tone `normal`): `retry_at` is
when it is tried again (the battery probe's or the resume's back-off end), `null` when it is tried as soon as there
is room; `cause` is `battery_shares_fuse` (a home battery charging from the grid) or `house_consumption`, as
`load_balancing_limited`'s. The plan's `plan_energy` and `plan_cost` follow it. The webhook says
`load_balancing_limited` (`limit_a` `0`, `phase` `null`, the same `cause`) in its place unless the request asks for
it (`"reads": ["balancing_paused"]`).

**A person's Stop nothing can see end.** A person's Stop ends when the car is unplugged. A charger that otherwise
says when a car is plugged in may say nothing while it is stopped (a Charge Amps connector turned off says
`Unavailable`, with or without a car); then the car's own plug sensor, read fresh since then, tells the unplug and
the next plug-in. A car whose only plug evidence is its charging state (a Skoda's `connect_cable`) tells them only by
a change of that state: a value it reports again says nothing. When neither can, the line `stop_unplug_unseen` (no params, tone `normal`) follows the `paused`
headline: only Start or Resume ends the Stop. It is a status line only, never a notification. The webhook leaves it
out unless the request asks for it (`"reads": ["stop_unplug_unseen"]`).

**The sun filling past a reached target.** Under Solar or Hybrid with a target, once the car is at the target and
below its own known charge limit, the sun may keep charging it to that limit. The status then says
`sun_past_target` (`percent`, the car's limit; tone `normal`): after the solar headline, and under Hybrid in place
of `hybrid_satisfied`. The webhook leaves it out after a solar headline and says `hybrid_satisfied` in its place
under Hybrid, unless the request asks for it (`"reads": ["sun_past_target"]`).

**A car held at its minimum on a lost measurement.** While the site's measurement has been stale or missing long
enough for load balancing to hold the car at its minimum current, `load_balancing_limited` carries `cause`
`measurement_lost` and `limit_a` that minimum. An app that does not know the value words it as a plain limit.

**The site's import target.** Since 2.3, while the site's import target holds a charge (see
[Holding the chargers under the import target](site-and-load-balancing.md#holding-the-chargers-under-the-import-target)),
the status carries these lines, tone `normal` but for `import_target_at_risk` (`notice`):

- Since the month's free level (`set_kw` on `import_target_held`, `import_target_paused` and `import_target_at_risk`):
  `target_kw` is the level held to this hour, the import target lifted to the demand charge's month peak already
  paid for where a part counts the hour; `set_kw` is the target as set. Both are the person's numbers: the hold keeps
  the hour under `target_kw` less a small margin (the larger of 0.1 kW and 2 %), shown only in the diagnostics
  (`hold_kw`). Equal where nothing lifts it.
- `import_target_held` `{limit_a, target_kw, hour_kw}` in place of `load_balancing_limited` while the target, not the
  fuse, holds the charging car: held to `limit_a` (amps, `null` when not known) so the hour stays under `target_kw`
  (kW, an hourly mean); `hour_kw` is the hour's mean import so far.
- `import_target_paused` `{until, target_kw}` in place of `balancing_paused` while the target paused the charge (a
  plan's or a person's Start): it waits for the next clock hour, `until`.
- `import_target_held` and `import_target_paused` carry `releasable` (`true`: offer "Release the import target
  for this charge", `release_import_target`, after a confirmation), `release_until` (the plan's departure ahead, or `null`:
  until the car is unplugged, the charge is done or noon) and `release_peak_kw` (what the month's peak could reach once set aside: the house's
  typical load and the charge's full power, kW rounded; `null` when not known), for that confirmation.
- `import_target_paused` carries `house_over` too: `true` where the house alone ends the hour above the target, said in
  place of "so the hour stays under it".
- `import_target_at_risk` `{departure, missing_kwh, target_kw, releasable}` after the issues while the plan cannot be
  ready by `departure` under the target: `missing_kwh` short. `releasable` (the plan's `if_not_ready` `ask`): offer
  "Release the import target for this charge", `release_import_target`; `false` (`hold`): only say it.
- `import_target_released` `{target_kw, departure, automatic}` after the issues while the charge runs with the target
  set aside until `departure`: by a person (`automatic` `false`), or by the plan's own `if_not_ready` `release`.
  `departure` `null`: set aside by a person until the car is unplugged (a Start, or a plan without a departure), the
  car ends the charge by itself, or the first local noon at least six hours after it was given, whichever comes
  first. A person's Start after a release keeps it, so (`departure` `null` from then on).
- `import_target_house_over` `{target_kw}` after the issues while the house alone ends the hour above the target and
  the car charges.

**Setting the import target aside.** `spotnav/release_import_target {"api_version": 1, "charger_id"}` (any user, as a
Start) and the webhook's `release_import_target` (optional `api_version`, `1`) set the site's import target aside for
that charger's charge until its plan's departure, or until the car is unplugged; without a departure ahead (a
person's Start, or a plan without one) until the car is unplugged, the car ends the charge by itself, or the first
local noon at least six hours later, whichever comes first. It is kept across a restart. The
answer is `{"api_version": 1, "ok", "error"}` (the webhook adds `"action": "release_import_target"`, HTTP 200, or 400
with the refusal): `spotnav_no_site`, `spotnav_no_import_target` (the site has none), or the charger lookup's codes. The set-aside also ends when the plan is made for another
departure. The phone's button on the notification does the same (its action id is
`SPOTNAV_IMPORT_TARGET_RELEASE_<charger id>_<departure, epoch seconds>`); a press for a departure that is no longer the
plan's sets nothing aside (`spotnav_stale_departure`, logged). The dashboard's `charger.capabilities` carries
`release_import_target`, `true` where the webhook answers the action.

A load-balancing limit or pause the target makes carries `cause` `import_target` (in `load_balancing_limited`, and in
`balancing_paused` with `retry_at` the next hour's start). The webhook gives an app that does not ask for them
(`"reads": ["import_target_status"]`) the lines it already words in their place: `load_balancing_limited`
(`limit_a` the same, `phase` `null`, `cause` `import_target`) for `import_target_held`; for `import_target_paused`
`balancing_paused` (`retry_at` its `until`) where it reads that, else `load_balancing_limited` with `limit_a` `0`;
and nothing for the other three. An app that does not know the cause words it as a plain limit.

**A car's charge limit range.** A vehicle row carries the additive `charge_limit_range`: `{"min", "max", "step"}`,
the percents `set_charge_limit` (and `spotnav/write_charge_limit`) can write to the car's own limit, or `null` when
there is no limit to write or its range is unknown. For a `number` limit these are the entity's own `min`, `max` and
`step` (a step of 1 when it states none; a range from 0 starts at its first step above it, since a limit is never 0 %);
for a percent `select`, its lowest and highest option and the smallest gap between two neighbours. A value outside
`min`..`max` is refused as before. Nothing is withheld for it: the released app reads a vehicle row key by key and
ignores keys it does not know. A client with no range offers 1-100 in whole percent and lets the write refuse the rest.

**Instant notifications.** `push_register` takes `push_ref`, the opaque reference the SpotNav relay
gave the app for its Firebase token (base64url text, at most 512 characters), or `null` to stop, and an
optional `events` (the notification event ids above; default `plan_stopped`, `plan_at_risk`,
`charge_complete`, `vehicle_identify`). It is stored per charger and kept across restarts; the answer is `{"ok": true,
"action": "push_register"}`, a bad body HTTP 400 `{"ok": false, "error": "invalid_push_register",
"action": "push_register"}`. When one of those events happens, Home Assistant posts `{"v": 1,
"push_ref"}` to the relay's `/v1/push/wake` (10 s, never retried, the same event not again within 15
minutes, at most twelve an hour), and the relay sends the app an empty wake-up; the app then reads
this charger as usual. Nothing about the charge goes to the relay. The question which car is plugged in
(`vehicle_identify`) has its own rules, since the app posts its own notification for it: every new question
wakes the app, and so does the question's end however it ends (answered on a phone, in the card or in the app,
decided by the cars or by a change of the target vehicle, swiped, unanswered after twelve hours, or the car
unplugged for longer than two minutes), so the app can take its notification down. Neither is held back by the
15-minute repeat rule or the hourly limit, though both count towards the limit; a plug-in asks at most once, so
a question wakes at most twice. One that comes while a wake-up is under way sends one more after it. Both are
the same empty wake-up: the app tells them apart by reading `identification`. A relay answer of 404
`unknown_ref` drops the reference. This is independent of the Companion phones chosen in
`notifications`. Examples are in `tests/fixtures/webhook/push_register_*.json`.

Turning **active load balancing** on or off is not available through the webhook, only through
the WebSocket by an administrator.

**Charger priority.** `update_charger_priority` takes `priority` and `expected`, each `"first"`,
`"normal"` or `"last"`, and an optional `api_version` (`1`); `expected` is the value the caller last
saw (the dashboard's `charger_priority.value`). It is validated and written by the same core as
`spotnav/update_entity_config`'s `charger_priority` field, and the charger is not reloaded for it. The
answer is `{"api_version": 1, "ok", "error", "field_errors", "charger_priority", "action"}`, where
`charger_priority` is the dashboard's block re-read after the write. HTTP 200 on success; 409
`spotnav_conflict` when `expected` is not the stored value (nothing is written); 400
`spotnav_invalid_value` with `field_errors` `[{"field": "charger_priority" | "expected", "code":
"invalid_value"}]` for a missing or unknown value; 400 `spotnav_no_site` (and `charger_priority: null`)
for a charger on no site; 400 `spotnav_unsupported_api_version`. Examples are in
`tests/fixtures/webhook/update_charger_priority_*.json`.

**Phase switching for solar.** `update_phase_switching` takes `enabled` and `expected`, both booleans, and an
optional `api_version` (`1`); `expected` is the value the caller last saw (the dashboard's
`phase_switching.value`). It is validated and written by the same core as `spotnav/update_entity_config`'s
`phase_switching` field, and the charger is not reloaded for it. The answer is `{"api_version": 1, "ok", "error",
"field_errors", "phase_switching", "action"}`, where `phase_switching` is the dashboard's block re-read after the
write (`null` where the setting is not offered). HTTP 200 on success; 409 `spotnav_conflict` when `expected` is not
the stored value (nothing is written); 400 `spotnav_invalid_value` with `field_errors` `[{"field": "phase_switching"
| "expected", "code": "invalid_value"}]` for a missing or non-boolean value, or `[{"field": "phase_switching",
"code": "not_writable"}]` for turning it on where it is not offered (turning it off is always allowed); 400
`spotnav_unsupported_api_version`. Examples are in `tests/fixtures/webhook/update_phase_switching_*.json`.

The webhook dashboard carries the additive `phase_switching` block, `{"value": bool, "writable": true}`, only where
the setting is offered for this charger (its integration can switch phases, it is wired for three phases and its
site has a solar source), and only when the request asks for it (`"reads": ["phase_switching"]`). The card reads the
same setting from its entity configuration instead.

**Electricity price.** The dashboard's additive `price_source` block (the electricity price this charger uses, the
choices, and the unanswered same-area question for an administrator) reaches the webhook only when the request asks
for it (`"reads": ["price_source"]`). Two writes twin the card's WebSocket commands for the webhook's own charger,
through the same cores:

- `choose_price_source {"price_entry_id"}`: the charger uses that electricity price; a charger on a site points its
  site at it, so it applies to every charger there, and one without a site points at it itself
  (`spotnav/choose_price_source`).
- `answer_price_duplicate {"area_id", "keep_entry_id"}`: the same-area question answered; with an entry, every site
  and charger using one of the others in the question moves to it (the others are kept), with `null` all are kept;
  either way it is not asked again (`spotnav/answer_price_duplicate`).

Both answer `{"ok", "error", "field_errors", "price_source", "action"}`, where `price_source` is this charger's
block re-read after the write (`null` while it prices with its own settings). HTTP 200 on success; 400
`spotnav_unknown_price_entry` (not an electricity price, or not one the question asks about),
`spotnav_unknown_price_duplicate` (no question for that area), or `spotnav_invalid_value` with `field_errors`
`[{"field": "price_entry_id" | "area_id" | "keep_entry_id", "code": "invalid_value"}]` for a missing field or one
of the wrong type; nothing is written on a refusal. Examples are in
`tests/fixtures/webhook/choose_price_source_*.json` and `answer_price_duplicate_*.json`.

**Grid tariff.** The dashboard's additive `grid_tariff` block sits beside `price_source` (only where the charger
prices with an electricity price) and reaches the webhook only when the request asks for it
(`"reads": ["grid_tariff"]`):

```json
{
  "entry_id": "price_a",
  "write_revision": 3,
  "transfer_percent": {"policy": "manual", "explicit_value": 5.0, "effective_value": 5.0, "unit": "%"},
  "transfer_high": {"policy": "off", "explicit_value": null, "effective_value": null, "unit": "öre/kWh"},
  "high_load_window": {"days": "weekdays", "from_month": 11, "to_month": 3, "from_hour": 7, "to_hour": 19},
  "demand": {
    "components": [
      {"window": {"days": "all", "from_month": 1, "to_month": 12, "from_hour": 6, "to_hour": 23},
       "top_n": 1, "price_per_kw": {"winter": 45.0, "summer": 23.0}}
    ],
    "available": true,
    "reason": null,
    "price_unit": "kr/kW",
    "per_component": [
      {"bar_kw": 6.975, "free_kw": 6.975, "price_per_kw": 23.0, "days_counted": 1,
       "peaks": [{"date": "2026-09-22", "start": "2026-09-22T07:00:00+00:00", "kw": 6.975}]}
    ],
    "hour": {"start": "2026-09-22T08:00:00+00:00", "kw_so_far": 3.1, "kwh_so_far": 1.033},
    "import_target_kw": 6.0
  },
  "writable": true
}
```

`transfer_percent` is a grid fee charged as a percentage of the spot price; `transfer_high` is the grid fee inside
the high-load window, which replaces the `transfer` fee there. `policy` is `off`, `manual` or `included` (the area's
published price already holds the grid fee: locked like `transfer`, nothing is added, whatever is stored);
`explicit_value` is what is stored (kept while off), `effective_value` what is added (`null` while off or
included). There is no `suggested` policy: no area suggests either figure. `high_load_window` is `null` or
`{"days", "from_month", "to_month", "from_hour", "to_hour"}`, all on the area's own clock: `days` is `all`,
`weekdays` (Monday to Friday; public holidays count as the weekdays they fall on) or `mon_sat`; `from_month` and
`to_month` are 1-12, both included, wrapping over the new year (11 and 3 is November to March, equal is one month,
1 and 12 is all year); `from_hour` 0-23 and `to_hour` 1-24 are whole hours, and a `from_hour` after `to_hour`
wraps over midnight (22 and 6 is 22:00-06:00); equal hours are refused. The hours after midnight of such a window
belong to the day it started on, for its days and its months alike: weekdays 22-06 holds Friday night until
Saturday 06:00 but not Sunday night. `write_revision` is the electricity price's write counter, which a write
names. See [Prices](prices.md#what-you-pay) for the arithmetic.

`demand` (since 2.3, inside this opt-in block) is the demand charge
and the import target, measured on the charger's site. `components` is the electricity price's demand-charge parts
as a write states them, at most three, `[]` for none: each `{"window", "top_n", "price_per_kw": {"winter",
"summer"}}`, where `window` is `null` (all hours count) or a window of `high_load_window`'s shape, `top_n` is 1, 3
or 5 (the mean of that many of the month's highest hourly means, each on a different day) and the prices are per
kW in the area's major unit, winter November to March, each `null` while unknown. `available` says whether the
site can measure them; `reason` is then `null`, else `no_site` (the charger is in no site) or
`needs_total_grid_power` (a direct site without the meter's total grid power); the codes are in
`tests/fixtures/codes/dashboard_codes.json` (`demand_reasons`). `price_unit` is the area's major unit per kW,
`null` while the area is not listed. `per_component` is aligned with `components` while the site measures, else
`[]`: for the month so far on the area's clock, `bar_kw` (the mean of the `top_n` highest day peaks, of fewer while
fewer days have one; `null` before any), `free_kw` (the hourly mean the running hour may reach without raising
`bar_kw`; `null` while the running hour does not count for the part), `price_per_kw` (this month's season's),
`days_counted` and `peaks` (`[{"date", "start", "kw"}]`, highest first; `date` is the day the hour counts under, a
night belonging to the day it starts on). An hour with more than ten minutes without a reading never sets a peak.
`hour` is the clock hour running now, `{"start", "kw_so_far", "kwh_so_far"}` (the mean import over the time
measured so far, and the energy), or `null` before it has a reading. `effective_target_kw` (since 2.3.0's
suggestion round, additive) is the level the running hour is held under: the import target lifted to the month's
peak where a part counts the hour and the month already reached more, else the target; `null` without a target or
where the site cannot measure. The webhook sends it only to an app that asks for it (`"reads": ["effective_target"]`,
in the dashboard and in `update_grid_tariff`'s answer). `import_target_kw` is the site's import target
(`null` for none), which is set with the site's `import_target_kw` field in `spotnav/update_entity_config` (a number
of at least 0.1, `null` or `""` to clear; written without reloading the site). Since 2.3 the import target holds
the site's chargers through load balancing (the status lines below); a demand charge is measured only. A full example is `tests/fixtures/grid_tariff/pilot_demand.json`.

The block also carries the electricity supplier's two figures (additive, 3.0), each
`{"policy", "explicit_value", "effective_value", "unit"}` with `policy` `off` or `manual` (never `included`) and
`unit` the area's minor unit per kWh: `supplier_markup`, the supplier's markup on each bought kWh, added before VAT
like the other fees (spot + markup + energy tax + grid fee, then VAT; a grid fee's percentage stays a percentage of
the spot price alone), and `sell_adjustment`, what a sold kWh earns beyond the spot price as it is paid out (the
grid operator's credit for production plus or minus the supplier's terms): never with VAT, and negative for a
deduction. The block also carries `sells` (boolean, read-only): whether anything is sold on this price, true when any
site whose chargers use this price entry has a home battery, a solar forecast, solar production statistics on the
Energy dashboard or a measured grid export (the meter's separate export entity). A client shows `sell_adjustment`
only where `sells` is true or the figure is on or has a stored value (never hide a figure in use), and shows it when
`sells` is absent. The webhook sends the two figures and `sells` only to an app that asks for them (`"reads":
["supplier_fees"]`, in the dashboard and in `update_grid_tariff`'s answer).

`update_grid_tariff {"expected_write_revision", "grid_tariff"}` replaces the three parts in full; `grid_tariff` is
`{"transfer_percent": {"enabled", "value"}, "transfer_high": {"enabled", "value"}, "high_load_window"}`, every key
required, and optionally `"demand_components"` (the list as `demand.components` states it, `[]` removes every
part; absent keeps the stored parts, so a writer that does not know them leaves them alone), and optionally
`"supplier_markup"` and `"sell_adjustment"` (each `{"enabled", "value"}`, absent keeps the stored figure; only
`sell_adjustment` may be negative). A part needs `top_n`
1, 3 or 5, a window of whole hours or `null`, and prices that are `null` or not negative; at most three parts. A fee that is on needs its figure, and the high-load fee needs a window (a window may stay while its fee
is off). It is the card's `spotnav/update_grid_tariff` for the webhook's own charger, through the same core, and
answers `{"ok", "error", "field_errors", "grid_tariff", "action"}` with the block re-read after the write. HTTP 200
on success; 409 `revision_conflict` (the electricity price was written since `write_revision`); 400
`invalid_fiscal` (with `field_errors` `[{"field": "transfer_percent" | "transfer_high" | "high_load_window" |
"demand_components" | "supplier_markup" | "sell_adjustment" | "grid_tariff", "code": "invalid_value"}]`), `invalid_number` (`expected_write_revision` is not a whole number) or
`spotnav_no_price_entry` (the charger prices with its own settings). Nothing is written on a refusal. The write
counts as a write of the electricity price: every charger using it replans, and their settings records' `revision`
moves with it. A write of the tariff as stored (all off where nothing is stored) passes the compare-and-set and
writes nothing: `write_revision` stays and nothing replans. The settings record itself never carries the grid tariff: its `overrides` rows keep exactly their
four keys, and a full `settings` replacement from a client that does not know the tariff leaves it as stored.

**A charger given a power.** The dashboard's `current_range` (`{min_a, max_a, source}`, whole amperes) carries an
additive `kw_per_a` for a charger SpotNav gives a power rather than a current (Sigenergy's DC charger): the kW one
ampere per phase is written as, `sqrt(3)` times the voltage between phases (0.6928 at 400 V). `max_a` is then the
charger's rating as a current per phase. The key is absent for every other charger, and sent to the webhook only when
the request asks for it (`"reads": ["power_charger"]`); a client without it shows the current in amperes, which stays
what `settings.amps` stores. The summary's `current_path` is then `power`.

**Where the prices come from.** The dashboard's `prices` block carries an additive `provider` object, sent to the
webhook only when the request asks for it (`"reads": ["price_provider"]`):

```json
"provider": {"key": null, "name": null, "url": null,
             "notice": {"code": "add_nordpool", "params": {}}}
```

`key` is `nordpool`, `entsoe`, `pvpc`, `octopus` or `zonneplan` (`null`, with `name` and `url`, when the area has no
source at all; since 2.0 no area is read from the relay, and a reader should treat an unknown key as no key); `null` for the whole object without an area. `notice` is `null` while the area has a source of its
own, else the one thing an administrator can do: `add_nordpool`, `nordpool_not_loaded`, `nordpool_custom` (Nord Pool from HACS stands in for Home Assistant's, which has the action SpotNav reads), `add_entsoe_key`,
`entsoe_key_rejected`, `add_pvpc`, `pvpc_not_loaded`, `add_octopus`, `octopus_not_loaded`, `octopus_no_agile`,
`octopus_region_mismatch` (`params.region`, the meter's region, whose rates are used), `add_zonneplan`,
`zonneplan_not_loaded`, `zonneplan_no_tariff`, `zonneplan_area` (a hint on another area of the Netherlands while
the Zonneplan integration is loaded: choose *Netherlands – Zonneplan*; shown only while that area has a working
source of its own, beside it, so an area without a source always gets its own notice first) or
`source_missing`. An area
with no source keeps showing the days it holds, with its notice. A source that is there but failing is no notice
(the prices are `stale` or `degraded` as before). `market.source` is the provider's attribution, `null` with no
source. `prices.index_state` and `prices.index_revision` are always `null` since 2.0 (there is no index); they stay
for readers that require them. The table is in `pricing/providers/notices.py`.

**What is left to set up.** The dashboard's additive `setup` block lists the card's "Get started" rows still left
for this charger, sent to the webhook only when the request asks for it (`"reads": ["setup"]`):

```json
"setup": {"items": [{"id": "site", "dismissible": true}, {"id": "app", "dismissible": true}]}
```

`id` is `vehicle` (no vehicle is set up for the charger), `car_level` (the plan charges to a target level, a
vehicle is set up and no source reports its level; never dismissible, and never beside `vehicle`), `site` (the
charger has no site), `solar_forecast` (the site can measure solar surplus and has no forecast source),
`demand_grid_power` (since 2.3: a demand-charge part or the site's import target is set and the site is a direct
one without the meter's total grid power; never dismissible), `grid_power_sign` and `grid_current_sign` (since 2.4.3:
the charger's site reads its grid power, or a direct site its signed phase currents, with the sign the wrong way round,
as the chargers' own steps show; never dismissible, and gone by itself once the steps agree or the setting is changed),
`phase_switching` (offered for the charger and off), `phone` (no phone is chosen for notifications and the paired
app has no push registration) or `app` (no charger's webhook has been used yet); a reader should skip an id it does not know. `spotnav/dismiss_setup_item {"item"}` (administrators
only) keeps a dismissible row away for the whole installation and answers `{"ok": true}`, else
`spotnav_setup_item_not_dismissible`. A grid sign row also carries `field` (the site option, `grid_power_inverted` or
`site_current_signed`) and `value` (what its button writes), e.g. `{"id": "grid_power_sign", "dismissible": false,
"field": "grid_power_inverted", "value": false}`: an administrator's button writes that one option with
`spotnav/update_entity_config` (`scope` `site`, `expected` the other value); nothing is written without it. The rows
are in `setup_checklist.py`.

**What the status line says about a plan.** Three facts are sent to the webhook only when the request asks for
them (`"reads": ["plan_facts"]`): the line `plan_saving` (`amount_minor`, `currency`), what a plan saves against
charging the same energy from now at the same power, beside `plan_cost` while nothing of the plan has started and
only from a 5 % difference; the line `solar_forecast` (`kwh`, `start`, `end`), the sun the site's forecast
promises a charger on the solar strategy for the rest of today, else tomorrow; and `hybrid_grid`'s `sun_start`
and `sun_end`, when the sun hybrid counts on is expected (null without a credit). Without the read the first two
are left out and `hybrid_grid` comes without the two keys.

**Prices for the app.** `price_days` answers the prices of the charger's electricity price (its price entry, else
its own area), read from what Home Assistant holds: no request is made for it. The day files and the profile are
the day documents SpotNav keeps ([prices](prices.md#the-day-document)), the format the relay published before 2.0,
so a client that read the relay reads them unchanged:

```json
{"ok": true, "action": "price_days", "v": 1,
 "area": {"id": "FI", "eic": "10YFI-1--------U", "countries": ["FI"], "name": "Finland",
          "tz": "Europe/Helsinki", "market_tz": "Europe/Brussels", "currency": "EUR", "major_unit": "€",
          "minor_unit": "cent", "vat_percent": 25.5, "suggested_tax": 2.325,
          "publication": {"time": "12:45", "tz": "Europe/Brussels"},
          "source": {"name": "Nord Pool", "url": "https://www.nordpoolgroup.com/"}},
 "provider": {"key": "nordpool", "name": "Nord Pool", "url": "https://www.nordpoolgroup.com/", "notice": null},
 "state": "ready", "reason": "ready", "waiting_for_tomorrow": false,
 "publication": {"time": "12:45", "tz": "Europe/Brussels"},
 "files": {"2026-10-29": {"v": 1, ...}, "2026-10-30": {"v": 1, ...}, "2026-10-31": {"v": 1, ...}},
 "profile": null}
```

- `area` is the area as the area list bundled with SpotNav states it (the contract-v2 shape), with the market calendar (`market_tz`, always
  written) and `publication` of the source its prices come from now, and that source as `source`.
- `provider` names that source, as the dashboard's `prices.provider` does (below).
- `state`, `reason` and `waiting_for_tomorrow` are the dashboard's `prices` block's.
- `files` are the held market-day files (day documents, keyed by market date) that cover today
  and tomorrow in the area's `tz`: two when its calendars agree, up to three when they do not (a Finnish day from
  Nord Pool's CET days, a London day from Paris days). A file not held yet is absent; cut the display days from the
  files by instant.
- `profile` is the history profile ([prices](prices.md#the-history-profile-document)), `null` until 21 of the last 28 days
  are held.

Refusals are HTTP 400 `{"ok": false, "error", "action": "price_days"}`: `spotnav_no_price_area` (nothing names an
area for this charger), `spotnav_unknown_price_area` (no area list knows it), `spotnav_no_prices` (no file for today
or tomorrow is held), `spotnav_unsupported_api_version`. The dashboard's `charger.capabilities.price_days` is `true`
where the webhook answers it, so a client asks only then. Examples are in
`tests/fixtures/webhook/price_days_*.json`.

**Plan preview over the webhook.** `plan_preview` is [`spotnav/plan_preview`](#plan-preview) for the paired app:
`{"version": 1, "action": "plan_preview", "settings": {...}}` with an optional `api_version` (`1`) and `reads`.
`settings` is the body the `settings` action takes (no `expected_revision`), judged by the same code as that write,
as an administrator, with what the app leaves out of it kept as its `settings` write keeps it (the withheld fields
above, and `8` for automatic periods without `"reads": ["auto_periods"]`), so the preview is the plan that write
then makes. Nothing is saved, installed, sent to the charger or the car, notified or fired, as for the command.

- A preview is HTTP 200 with the command's answer and the routing `action`:
  `{"api_version": 1, "ok": true, "error": null, "preview": {"state", "reason", "plan", "status", "strip"}, "action": "plan_preview"}`.
  The `status` lines are the ones the app reads in the dashboard (`reads` as for `dashboard`: `plan_saving` only
  with `plan_facts`, and so on).
- A body a Save would refuse is HTTP 400 with the same envelope: `{"api_version": 1, "ok": false, "error":
  "invalid_amps", "preview": null, "action": "plan_preview"}` (the Save's own code).
- Refused before the body is judged, `{"ok": false, "error", "action": "plan_preview"}`: HTTP 429
  `spotnav_rate_limited` with `"retry_after_s": 1` and `Retry-After: 1` (more than 5 requests in a second on this
  charger's webhook, or 4 previews being calculated at once across the card and the app); HTTP 400
  `spotnav_body_too_large` (`settings` over 16 KiB as JSON), `spotnav_unsupported_api_version`, and the charger
  lookup's codes.

**Get started over the webhook.** `dismiss_setup_item {"item"}` is `spotnav/dismiss_setup_item`: the row stays away
for the whole installation, as from the card. `{"ok": true, "action": "dismiss_setup_item"}`, or HTTP 400
`{"ok": false, "error": "spotnav_setup_item_not_dismissible", "action": "dismiss_setup_item"}` for a row that cannot
be dismissed (`car_level`), an unknown one or an `item` that is not a text.

**The charger's own setting over the webhook.** `fix_own_mode {"entity_id", "value"}` (optional `api_version`, `1`)
is [`spotnav/fix_own_mode`](#websocket-commands) for this webhook's charger, by the same core: the write is made only
when, read again now, `entity_id` is one of this charger's conflicts (a `charger_own_mode` line's `entity`, or the
`charger_disabled` line's enable switch) and `value` is that conflict's own fix (the line's `value`, `on` for the
enable switch). Nothing else is ever written: another entity of the charger, another device's entity, an entity no
profile names, another value or a conflict that has ended are all refused. The answer is the command's with the
routing `action`: HTTP 200 `{"api_version": 1, "ok": true, "error": null, "action": "fix_own_mode"}` once Home
Assistant has written it (the line clears once the entity reads the new value); HTTP 400 with `"ok": false` and
`spotnav_no_conflict` or `spotnav_unsupported_api_version`; HTTP 502 with `spotnav_own_mode_failed` (the charger's
integration refused the write). Each write is logged at INFO.

**Capabilities of the webhook.** The dashboard's `charger.capabilities` has `plan_preview`, `dismiss_setup_item` and
`fix_own_mode` beside `price_days`, `true` where the webhook answers that action. An older Home Assistant leaves them
out (and answers the action `Unsupported action`), so an app offers the feature only where its key is `true`.
`site_import_target` is `true` where `update_site_settings` takes `import_target_kw` (below). Since 3.0 the list
ends with `get_house`, `get_heat`, `battery_control` and `get_site_settings_view`: `true` where this charger's
webhook answers the site's actions (the charger is in a loaded site), `false` for a charger in no site, and absent
from a 2.x Home Assistant (see [the site over the webhook](#the-site-over-the-webhook)).

**Where a meter belongs.** Since 3.0 `spotnav/update_site_settings` takes `meter_belongs` in `changes`: `{statistic
id: "house" | "heat" | "car" | "none"}` for Energy dashboard individual devices, merged into the site's stored choices
(`house`, the default, removes one; at most 64 kept), written without a reload. `expected` may name the whole stored
mapping as last read (`{}` for none; `get_house_loads` answers it as `belongs`): another is `spotnav_conflict`. An
unknown place or an empty mapping is `spotnav_invalid_value`. The answer carries `meter_belongs` (the mapping stored
now) beside `site`. WebSocket only, administrators only: over the webhook it is `spotnav_not_permitted_over_webhook`.

**The site's import target over the webhook.** Since 2.3 `update_site_settings` (and `spotnav/update_site_settings`)
takes `import_target_kw` in `changes`: a JSON number from 0.1 to 1000 (kW, an hourly mean), or `null` to clear it.
It is stored as the card's `spotnav/update_entity_config` stores it and written without reloading the site (the
target is read on every recompute). `expected` may name it, the value last seen (`null` for none): a different
stored value is `spotnav_conflict` (HTTP 409). A string, a boolean or a number out of bounds is
`spotnav_invalid_value` (HTTP 400) and nothing is written. It may go with the solar fields in one write. A request
that names it is answered with `import_target_kw` beside `site` (the value stored now, `null` for none), success or
refusal:

```json
{"action": "update_site_settings", "expected": {"import_target_kw": null}, "changes": {"import_target_kw": 6.0}}
→ {"api_version": 1, "ok": true, "error": null, "site": {…}, "restore": null, "import_target_kw": 6.0,
   "action": "update_site_settings"}
```

A request that does not name it is answered as before and leaves the stored target as it is, so an older app's
write keeps it. The dashboard shows it in `grid_tariff.demand.import_target_kw`. Full example:
`tests/fixtures/webhook/update_site_settings_import_target.json`.

**Permissions over the webhook.** The webhook id is the credential: whoever holds it acts as an administrator for
its own charger (the dashboard's `writable` flags are true), so these writes, like `update_site_settings` and
`update_charger_priority`, need nothing more. What stays WebSocket-only, for a signed-in administrator: the control
opt-ins (active load balancing, the battery control's switch and its other options, the heat control's switch) and
choosing the entities SpotNav uses (the heat sensors, zones and control path among them). Naming one of them over the
webhook is `spotnav_not_permitted_over_webhook` (HTTP 403) and nothing is written.

### The site over the webhook

Since 3.0 a charger's webhook also answers for the charger's **site**: the house, the heating and the home battery,
which the [house card](house-card.md) and the heat card read over the WebSocket. Each action is the twin of a
WebSocket command and answers that command's own document for the same moment, with `"ok": true` and the routing
`"action"` beside it (`tests/test_webhook_site_parity.py` asserts the two equal). The site is always the webhook's
own charger's: a `site_id` or `charger_id` in the body is not read. `api_version` is optional in every body (`1`).

| Action | Twin | Body | Example (`tests/fixtures/webhook/`) |
| ------ | ---- | ---- | ------- |
| `get_house` | `spotnav/get_house` | `date` (`YYYY-MM-DD`, optional: a past day) | `get_house_success.json`, `get_house_no_site.json` |
| `get_house_now` | `spotnav/get_house_now` | | `get_house_now_success.json` |
| `get_house_history` | `spotnav/get_house_history` | `period` (`day`, `month`, `year`), `date` (optional: today) | `get_house_history_success.json`, `get_house_history_invalid_date.json` |
| `get_house_loads` | `spotnav/get_house_loads` | `period`, `date`, `kind` (`house`, `heat`), all optional | `get_house_loads_success.json` |
| `get_heat` | `spotnav/get_heat` | | `get_heat_success.json` |
| `get_heat_history` | `spotnav/get_heat_history` | `period`, `date` (optional) | `get_heat_history_success.json` |
| `update_heat` | `spotnav/update_heat` | `changes`, `expected` | `update_heat_success.json`, `update_heat_away.json`, `update_heat_away_delta.json`, `update_heat_night.json`, `update_heat_zone_offsets.json`, `update_heat_indoor_offsets.json`, `update_heat_indoor_offsets_not_permitted.json`, `update_heat_conflict.json`, `update_heat_invalid_value.json`, `update_heat_not_permitted.json`, `update_heat_zones_not_permitted.json` |
| `battery_control` | `spotnav/battery_control` | `changes` (optional) | `battery_control_success.json`, `battery_control_mode.json`, `battery_control_invalid_value.json`, `battery_control_not_permitted.json` |
| `get_site_settings_view` | `spotnav/get_site_settings_view` | | `get_site_settings_view_success.json` |

Richer examples of each document (a villa with sun, a battery and a heat pump) are the WebSocket shapes in
`tests/fixtures/house/`, `house_now/`, `house_history/`, `house_loads/`, `heat_card/`, `heat_history/`,
`battery_control/` and `site_view/`.

**Refusals of the reads** are HTTP 400 with `{"ok": false, "error", "action"}` and the command's own stable code:
`spotnav_unsupported_api_version`, `spotnav_no_site` (the charger is in no site, or the site is not loaded),
`spotnav_invalid_date` (a date or period that cannot be shown), `spotnav_invalid_kind`, `spotnav_read_failed` (the
pie's statistics could not be read).

**Detecting it.** `charger.capabilities` in the dashboard: `get_house` (with it `get_house_now`,
`get_house_history`, `get_house_loads`), `get_heat` (with it `get_heat_history`, `update_heat`), `battery_control`
and `get_site_settings_view`. A 2.x Home Assistant has none of the keys and answers the actions `Unsupported action`.
What a site has to show is in the answers themselves: `get_house_now`'s `has` (`sun`, `battery`, `car`, `heat`),
`get_site_settings_view`'s `battery.found`, and in `get_heat` a `meter`, a `pump`, `zones` or indoor sensors.

**`get_house_now` is made to be polled.** It reads what the site already holds in memory (the grid power, the
chargers' draw, the battery's facts, the power sensors' states); the one recorder read behind it, the day's energy
flow for a site whose sun or heat has no power sensor, is kept until the recorder's next 5-minute row. The answer is
about 0.3 to 0.4 kB and takes about 0.1 ms to build. An app may ask every 10 s while its Now tab is in view; the
other reads are heavier (`get_house` and `get_heat` are 8 to 20 kB, a history 3 to 16 kB) and are meant
for once on opening a tab and then about once a minute.

**`get_heat`** answers the heat card's whole view (`heat/view.py` has the shape): `config` (the settings below),
`comfort` (the band around the target now), `indoor` (the measured value, the coldest room, each sensor), `outdoor`,
`zones`, `pump`, `meter`, `today` (the day's heat, hot water and `cost`, with `money`), `status` (what the pump does
and what SpotNav would do or does), `band` (the day's plan by the hour), `control`, `relay`, `review`, `indoor_day`,
`notes`, `ahead` (the next 12 hours of the plan) and `shadow` (`false` only while SpotNav's heat control runs). Over the webhook `writable` is `false` and
`choices` is `null`: they describe the configuration (sensors, zones, control path), which is set in Home Assistant.
The dial and home or away are everyone's, whatever `writable` says.

**`get_heat_history`** answers one day, month or year of the heating (`heat/history.py` has the shape): `energy`,
`buckets` (an hour, a day or a month each, with its `parts` and `saving`), `parts`, `saving`, `degree_days`,
`comfort`, `shadow`, `runs`, `review`, `indoor_day`, `model` and `money`. `period` and `date` as
`get_house_history`'s.

**`update_heat`** writes the everyday choices of the heat card, judged as `spotnav/update_heat` judges them:

- `target_c`: 16 to 25 in steps of 0.5 (`out_of_range`, `not_a_step`, `invalid_value`).
- `mode`: `home` or `away`; going away needs `away_until`, an ISO instant with its offset, in the future and at
  most 90 days ahead (`away_until_required` and the range codes). Home clears it.
- `away_delta_c` (the setback while away): 1 to 6 in steps of 0.5, 3 by default, `null` returns to 3
  (`out_of_range`, `not_a_step`, `invalid_value`). Away holds the target less this, never below 16 °C; the warm-up
  before the return is unchanged. `get_heat` reads it as `config.away_delta_c`.
- `night` (the cooler night): `{"on", "delta_c", "start", "end"}`, `delta_c` 0.5 to 3 in steps of 0.5, the times
  `HH:MM`; a key left out keeps its stored value.
- `zones`: each zone's offset against the house temperature, `[{"entity_id", "offset_c"}]` with `offset_c` -3 to 3
  in steps of 0.5. The list must name exactly the zones stored (`config.zones`), in their order: which entities
  are zones is configuration, and a list that adds, drops, swaps or reorders one is
  `spotnav_not_permitted_over_webhook`.
- `indoor_offsets`: each indoor sensor's own aim against the house temperature, `[{"entity_id", "offset_c"}]` with
  `offset_c` -3 to 3 in steps of 0.5. The list is sparse, as `config.indoor_offsets` is: a sensor left out or at 0
  has no offset, and `null` clears them all. Every `entity_id` must be one of the stored `config.indoor_sensors`;
  naming any other entity is `spotnav_not_permitted_over_webhook`. Which sensors are read never changes by it.
- `expected` (optional) names the values last read; a stored value that differs is `spotnav_conflict` and nothing
  is written.

The answer is the command's envelope with the `action`: `{"api_version": 1, "ok", "error", "field_errors":
[{"field", "code"}], "config", "action": "update_heat"}`, `config` re-read after the write. HTTP 200 written; 400
`spotnav_invalid_value` (with `field_errors`; an unknown field is `unknown_field`); 409 `spotnav_conflict`; 403
`spotnav_not_permitted_over_webhook` for any other heat setting, alone or beside an allowed one:
`control_enabled` ("Styr värmen"), the control path and its entities, the hot water entity, which sensors are read
(`indoor_sensors`, `outdoor_sensor`), which entities are zones, and the meter are written in Home Assistant by an administrator.

```json
{"version": 1, "action": "update_heat", "changes": {"target_c": 21.5}, "expected": {"target_c": 21.0}}
→ {"api_version": 1, "ok": true, "error": null, "field_errors": [], "config": {"target_c": 21.5, …},
   "action": "update_heat"}
```

**`battery_control`** answers `{"api_version": 1, "ok", "error", "status", "action": "battery_control"}`, `status`
the control's state, its one line, the options, what it needs and its notes (see [home batteries](home-battery.md);
`tests/fixtures/battery_control/` has a plant under control). With `changes` it writes first, and the only key the
webhook takes is `battery_mode`: `self_consumption`, `balanced` or `max_profit`. HTTP 200 written (the plan is made
again at once); 400 `spotnav_invalid_value` (another mode, an unknown key, no changes); 403
`spotnav_not_permitted_over_webhook` for `battery_control_enabled` ("Styr batteriet") and the other
`battery_control_*` options, alone or beside a mode, with nothing written; 400 `spotnav_no_site`. A refusal has
`"status": null`. An app shows the switch read-only (`status.options.battery_control_enabled`, `status.state`).

**`get_site_settings_view`** answers the settings' site block as an administrator reads it, with one difference
the webhook's dashboard also has: `site.active_control.writable` is `false`. `writable` is `true` for what the
webhook's own writes change (`update_site_settings`, `choose_price_source`, `update_grid_tariff`); the price's
fees (`price_settings.fees`) are shown but have no webhook write.

**`plug_in_saving` on `sessions`.** With `"reads": ["plug_in_saving"]` the webhook's `sessions` answer is the
WebSocket's whole: every summary (`this_month`, `last_month`, `months`, `days`, `month_summary`, `month_days`) and
every session has `plug_in_saving`, what SpotNav saved against charging at once on plug-in (major unit; `null` where
it cannot be known, such as a session from before plug-in times were kept). The keys are absent for a charger in no
loaded site and for a request that does not read them. Examples: `sessions_plug_in_saving.json`,
`sessions_success.json`.

## WebSocket commands

Every command takes `api_version: 1` and (except `list_chargers`) a `charger_id`, the charger's
config entry id. Reading is open to every authenticated user, and so is `spotnav/manual_action`
(Start, Stop, Pause, Resume); `spotnav/update_settings` is an administrator's, or, where the charger's
`users_change_plan` allows it, anyone's for the strategy and plan fields alone (see **Who may replace the settings** under
[Charger webhook](#charger-webhook)); every other write requires an administrator
(`spotnav_not_admin`, or Home Assistant's own `unauthorized`, otherwise). The dashboard's `control.can_act` says whether the
caller may use `manual_action` (true for every signed-in user and over the webhook); `site.writable`
and `charger_priority.writable` say whether it may change those (an administrator, or the webhook).

| Command | Purpose |
| ------- | ------- |
| `spotnav/list_chargers` | The chargers on this instance (`id`, `name`). |
| `spotnav/get_dashboard` | The dashboard document for one charger. |
| `spotnav/get_house` | The [house card's](house-card.md) read-only view of one site (a shadow preview): `{"api_version": 1, "charger_id" or "site_id", "date"}`, `date` (a day, `YYYY-MM-DD`) optional for a past day. `battery.now` `{action, power_kw}` is what the battery does now, for the card's header: `action` the action SpotNav's battery control runs (`grid_charge`, `hold`, `discharge_house`, `export_discharge`, …; `null` when it runs none) and `power_kw` the battery's measured power (+ charging, `null` unread); absent from a backend before it. |
| `spotnav/get_house_now` | The [house card's](house-card.md#now) live flows for its Now tab, read-only: `{"api_version": 1, "charger_id" or "site_id"}`. Answers `{api_version, site_id, at, grid_kw, sun_kw, sun_from, battery_kw, soc_pct, car_kw, car_connected, car_soc_pct, heat_kw, heat_from, house_kw, has: {sun, battery, car, heat}}`: `grid_kw` the net grid power now (+ import), `battery_kw` the battery's (+ charging), `car_kw` what the chargers draw, `car_connected` whether a car is plugged in (`null` without chargers), `car_soc_pct` its level where its plan's vehicle reads one, `sun_kw` the Energy dashboard's solar power sensors summed (`sun_from: "power"`) or the last energy-flow period's mean when it ended within 20 minutes (`"recent"`), `heat_kw` likewise from the heat pump's meter, `house_kw` the whole house (the heat included) as grid + sun − battery − cars, never below 0; each `null` unread, and `has` the parts the site has. The card polls it every 30 s while the tab is shown. Refusals as `get_house`'s. |
| `spotnav/get_house_history` | One day, month or year of the house for the card's history and its period tabs: `{"api_version": 1, "charger_id" or "site_id", "period", "date"}`. Each of its `buckets` (an hour, a day or a month) has, besides the energy, the cost and the reviews, `arbitrage`: what the battery earned in it, the card's headline measure (minor unit; each kWh it gave back over what that energy cost to store, as `battery/stored_value.py` keeps it: by the hour 40 days back, by the day 400 days back; `null` without a battery or where nothing is kept), `battery_flow`: what that is made of, `{discharged, worth, cost, charged, charge_cost}` (kWh and minor units; the energy given back, what it was worth, what it cost out of the stock, and the energy charged with its cost; kept as `arbitrage` is, `null` where it is not), and `buy`, `sell`: the mean prices over its priced periods (minor unit per kWh, weighted by minutes; `null` unpriced). The full shape is in `site/house_period.py`. |
| `spotnav/get_house_loads` | What the house's known meters used over one period, for the [house card's](house-card.md#what-the-house-used) pie: `{"api_version": 1, "charger_id" or "site_id", "period", "date", "kind"}`, `period` `day` (the default), `month` or `year`, `date` a day inside it (absent: today), `kind` `house` (the default) or `heat`. Answers `{api_version, site_id, kind, period, date, end, through, house_kwh, meters_kwh, other_kwh, over_kwh, loads: [{statistic_id, name, kwh, source, changed}], moved: [{statistic_id, name, belongs}], belongs: {statistic_id: belongs}, implausible: [{statistic_id, name, hours, left_out}]}`: `house_kwh` is the energy flow's house block (with `kind: "heat"` its heat block) for the period (`null` without the Energy dashboard's grid meter); `loads` the Energy dashboard's individual devices (largest first) less those sorted out by themselves (a charger's sensor or device, the heat pump's meter, a heating zone's sensor or device), any nested in another device and any the household moved (`moved`, with where to: `heat`, `car` or `none`); with `kind: "heat"` the heat pump's meter (`source` `heat_pump`), each heating zone counted (`zone`) and each meter moved to heat (`meter`, `changed` true). Read from the recorder's statistics up to `through`; an hour above what the block used in it (+0.2 kWh) or above the main fuse is not counted and the meter is in `implausible` (`left_out` when no hour was left). `other_kwh` the block less them, never below 0, and `over_kwh` how much they exceed it; `belongs` the stored choices (the `expected` of a write). Read-only; refusals as the house history's, `spotnav_invalid_kind` for another `kind`, and `spotnav_read_failed` when the statistics cannot be read. |
| `spotnav/get_heat` | The heat card's view of one site: `{"api_version": 1, "charger_id" or "site_id"}`. The shape is in `heat/view.py`; `writable` and `choices` (the entities each setting may be set to) only for an administrator. Refusals as `get_house`'s. Over the webhook: `get_heat` (see [the site over the webhook](#the-site-over-the-webhook)). |
| `spotnav/update_heat` | Change the site's heat settings with a compare-and-set: `{"api_version": 1, "charger_id" or "site_id", "changes", "expected"}`, keys `target_c`, `mode`, `away_until`, `away_delta_c`, `night`, `indoor_sensors`, `indoor_offsets`, `outdoor_sensor`, `control_path`, `control_entity`, `control_entity_b`, `hot_water_entity`, `zones`, `control_enabled`, `heat_meter_entity`. Answers `{api_version, ok, error, field_errors, config}`; `error` is `spotnav_conflict`, `spotnav_invalid_value` (with `field_errors`) or `spotnav_not_admin` (someone who is no administrator named anything but `target_c`, `mode`, `away_until`). `control_path` may also be `sg_ready_api` (NIBE S series: `control_entity` the mode's number, `control_entity_b` its activation switch). With `"enable_control_entities": true` (an administrator's confirm of the answer's `control.enable_entities`, never over the webhook) the stored control path's disabled entities are enabled and their integration reloaded once before the write; `spotnav_enable_failed` when that could not be done. Over the webhook: `update_heat`, those three keys, `away_delta_c`, `night` and the offsets of the stored zones and indoor sensors only. |
| `spotnav/get_heat_history` | One day, month or year of a site's heating: `{"api_version": 1, "charger_id" or "site_id", "period", "date"}`, as `get_house_history`. The shape is in `heat/history.py`. Over the webhook: `get_heat_history`. |
| `spotnav/get_settings`, `spotnav/update_settings` | Read and replace the settings record. |
| `spotnav/manual_action` | `start`, `stop` (with an optional pause `choice`) or `resume`; any signed-in user. |
| `spotnav/plan_preview` | What a Save would plan, with nothing saved (see [Plan preview](#plan-preview)). |
| `spotnav/get_market_options` | Price areas from the area list bundled with SpotNav and their suggested fiscal values. |
| `spotnav/find_region` | A Great Britain postcode to its price region (`GB-A` … `GB-P`): `{"postcode": "SW1A 1AA"}` answers `region` (one), `regions` (the listed ones) and `reason` (`null`, `invalid_postcode`, `not_found`, `unavailable`). Home Assistant asks Octopus Energy's public lookup directly; the postcode never reaches the relay and is neither stored nor logged. |
| `spotnav/get_entity_config`, `spotnav/update_entity_config` | The entities a charger and its site use. With `site_id` in place of `charger_id` (a site with or without chargers), the site's alone: only the scope `site`, `charger_id` and `control` `null`, `vehicles` empty; an unknown id is `spotnav_unknown_site`, an unloaded site `spotnav_no_site`. |
| `spotnav/list_sites` | The loaded sites: `{"api_version": 1, "sites": [{"site_id", "name", "charger_ids"}]}`, by name. |
| `spotnav/get_site_settings_view` | The settings popover's site block, by `site_id` or a `charger_id` in the site: `{"api_version": 1, "site_id", "name", "charger_ids", "writable", "price": {"entry_id", "title", "area_id", "area_name"} or null, "battery": {"found"}, "setup": {"items": [...]}, "price_settings", "site"}`. `price_settings` (`null` exactly where `price` is) is the popover's price section: `market` (the area's facts), `fiscal`, `fees` (the price's stored row, `{area_id, vat, tax, transfer}`), `provider`, `price_source` and `grid_tariff` as the dashboard states them, read for the site. `site` is the site's own `site` block (solar priority, forecast sources, load balancing), `null` while the site is not running. `setup` has the site's own Get started rows in the dashboard's shape: `charger` (no charger; dismissible), `price` (no electricity price applies), `grid_power_sign`, `grid_current_sign`, `solar_forecast`, `demand_grid_power`, `battery` (a battery found, control off; dismissible), `heat` (a heat pump found, no heat settings; dismissible). Refusals `spotnav_unknown_site`, `spotnav_no_site` and the charger lookup's. |
| `spotnav/battery_control` | The site's battery control status, after writing `changes` when given (see [home batteries](home-battery.md)); by `site_id`, or by `charger_id` for that charger's site. |
| `spotnav/choose_vehicle_soc` | Choose (or clear) a vehicle's state-of-charge sensor. |
| `spotnav/update_vehicle` | A vehicle's battery capacity, consumption, onboard charger, target, minimum charge level and wake for a reading. |
| `spotnav/write_charge_limit` | Write a car's own charge limit (`vehicle_id`, `percent`), the webhook's `set_charge_limit` over the WebSocket: same entity, range checks and one write a minute per car. Answers `{"api_version": 1, "ok", "error", "retry_after_s"}`; refusals `spotnav_invalid_value` (not a car with a writable limit, or a percent it cannot take), `spotnav_too_soon` (with `retry_after_s`), `spotnav_charge_limit_failed` (the car's integration failed the write). |
| `spotnav/fix_own_mode` | Set one of the charger's own settings back (administrators only), the card's button on the status lines `charger_own_mode` (`entity`, `label`, `state`, `value`: one of the charger's own modes is on and works against SpotNav; `value` is the one value that ends it, `null` where there is no single safe one; `entity_name`, `state_name`, `value_name`: Home Assistant's name of the entity and the display names of `state` and `value` in its configured language, for a client without its frontend, the id or the raw value where it names none, `value_name` `null` with `value`) and `charger_disabled` (`entity`: its own enable switch, turned on; `entity_name` its name, `null` with `entity`). `{"api_version": 1, "charger_id", "entity_id", "value"}` is written only when, read again, that entity is still a conflict of this charger (an entity of its own device and integration that its profile names) and `value` is that conflict's fix: `select.select_option`, `switch.turn_off` or, for the enable switch, `switch.turn_on`. Answers `{"api_version": 1, "ok", "error"}`; refusals `spotnav_no_conflict` (any other entity or value, or a conflict that has ended) and `spotnav_own_mode_failed` (the charger's integration refused the write). Each write is logged at INFO. The webhook leaves `charger_own_mode` out unless the request asks for it (`"reads": ["own_mode_status"]`), and the block's tone is then its strongest line left. |
| `spotnav/update_site_settings` | Car or home battery first (`solar_priority`), forecast sources, the import target, active load balancing, where meters of the house pie belong (`meter_belongs`, below). With `site_id` in place of `charger_id` (a site with or without chargers), the site itself; the answer's `site` block is then the site's own. |
| `spotnav/get_market_options`, `spotnav/choose_price_source` | The area list with the stored area, and the electricity price a charger uses. With `site_id` in place of `charger_id` (or `entry_id`): the area of the price the site reads, and pointing the site at a price (its chargers follow). |
| `spotnav/update_price` | The area and fees of the electricity price a site reads (administrators only): `{"site_id", "expected_write_revision", "price": {"area_id", "vat", "tax", "transfer"}}`, the row as the settings body states it, compare-and-set on the price's write counter (`grid_tariff.write_revision`). Answers `{"ok": true}`, or the codes `revision_conflict`, `invalid_area`, `invalid_fiscal`, `invalid_number`, `missing_field`, `unknown_field`, `spotnav_no_price_entry`, `spotnav_unknown_site`, `spotnav_not_admin`. The charger card writes these through the charger's settings; the app has no twin. |
| `spotnav/update_grid_tariff` | The grid tariff of the charger's electricity price (administrators only): `{"charger_id", "expected_write_revision", "grid_tariff"}` as the webhook's `update_grid_tariff` (see **Grid tariff** under [Charger webhook](#charger-webhook)). Answers `{"ok": true, "grid_tariff": {...}}` with the block re-read, or the error codes `invalid_fiscal`, `invalid_number`, `revision_conflict`, `spotnav_no_price_entry`, `spotnav_not_admin` and the charger lookup's. With `site_id` in place of `charger_id`, the price the site reads (a site without chargers too), answered with the site's block. |
| `spotnav/get_debug_bundle` | The redacted installation-wide debug bundle (administrators only). |
| `spotnav/send_debug_info` | Send that same bundle to SpotNav's developer (administrators only; a non-administrator gets Home Assistant's `unauthorized`): `{"api_version": 1, "message", "email"?, "client"?}`, `message` required (at most 4000 characters), `email` optional (at most 254), `client` the card's own block (its known keys only). Sent only when this command is called, gzip-compressed, to `https://spotnav.sensnology.se/v1/support/report`, with the installation as a salted SHA-256 of Home Assistant's instance id, never the id. Answers `{"api_version": 1, "ok", "error", "ref", "retry_after_s"}`: `ref` the reference to quote (`SN-XXXX`); refusals `spotnav_report_invalid_message`, `spotnav_report_invalid_email`, `spotnav_report_rate_limited` (with `retry_after_s`), `spotnav_report_too_large`, `spotnav_report_rejected`, `spotnav_report_network`, `spotnav_report_server`. |
| `spotnav/get_blueprints` | How many automations use each of SpotNav's [blueprints](automations.md) (administrators only): `{"api_version": 1, "ok": true, "error": null, "blueprints": [{"file", "path", "automations"}]}`, one row per blueprint, `path` being `spotnav/<file>` as an automation refers to it. A non-administrator gets `spotnav_not_admin` and no rows. Not a dashboard field, so the app is never handed a key it does not know. |
| `spotnav/get_card_info` | Which card the integration serves, for any signed-in user: `{"api_version": 1, "ok": true, "error": null, "spotnav_version", "card_bundle_hash"}`. The card compares the hash with the one in the URL it was loaded from. Not a dashboard field, so an older card is never handed a key it does not know. |
| `spotnav/identify_vehicle`, `spotnav/choose_vehicle_identification` | Answer "which car is plugged in?", and choose a car's plug or location source (see [vehicle identification](#vehicle-identification)). |
| `spotnav/update_guest` | The guest amount, administrators only (see [the guest car](#the-guest-car)). |
| `spotnav/get_sessions` | A charger's charge sessions: summaries per month and day and the latest sessions, the same for one chosen `month`, or with `format: "csv"` and optional `from` and `to` dates (or a `month`) the sessions of that range as CSV text. |
| `spotnav/get_power_month` | One month of a site's measured import, by day and hour, against its demand-charge parts and import target (see [Power by month](#power-by-month)). |
| `spotnav/suggest_import_target` | A suggested import target from Home Assistant's statistics (administrators only; see [A suggested import target](#a-suggested-import-target)). |

Rules that hold across them:

- **Full replacement, not patches.** Settings are replaced in full and name the `revision` they
  edited (`expected_revision`); a stale write returns `revision_conflict` with the current record.
  Other writes carry `expected` values and answer `conflict` with the current state.
- **Stable codes, no prose.** Refusals are codes (`spotnav_unknown_charger`,
  `spotnav_charger_unloaded`, `spotnav_invalid_value` with per-field `field_errors`, and so on);
  clients switch on the code, never on a message.
- **Absence is not zero.** A missing value is `null`; nothing is coerced.
- **The answer is a re-read**, captured after the write settles, never assembled from what the
  caller sent.

### Plan preview

`spotnav/plan_preview` answers what the charger's plan would be if a settings body were saved, and saves nothing.
The card's Plan dialog asks it about 300 ms after each change, so a person sees the plan before pressing Save.

Request: `{"type": "spotnav/plan_preview", "api_version": 1, "charger_id", "settings"}`, where `settings` is the
same body `spotnav/update_settings` takes (no `expected_revision`). The body is judged by the same code as a
write: the same decoding, the same fields kept when left out, the same rule for a user who is not an
administrator (`spotnav_not_admin` unless `users_change_plan` allows the change), the charger's current range and
the departure date's limits. The plan is the controller's own calculation on the record that write would store:
the price source's area and fees, the learned power and the count of a manual need, exactly as a Save calculates
it at the same moment.

Nothing happens. No settings or revision are written, no plan is installed and no proposal saved, no appointment
is made, the charger and the car are asked nothing, and no notice, event or listener hears of it.

While an electricity price entry is the charger's price source, the preview plans with that entry's area and fees as
they stand, as a Save of a body that echoes them does; a body that changes them (an administrator's) is previewed with
the entry's current ones.

A few small differences from the calculation a Save starts, all about state the preview may not touch:

- A Save first lets the charger end a target stop's record that no longer describes the planned car
  (`_lapse_target_stop`); the preview does not, so right after such a change its status may differ from the Save's.
- A Save first reads the energy register once and counts a reading nobody saw yet (`_arm_energy_watch`); the
  preview counts a manual need from what the register was last seen to say.
- The phases a charge uses come from the stored planned car, so a preview that picks another car with fewer
  onboard phases plans on the stored car's.

At most 5 requests a second per connection and 4 previews being calculated at once across all connections
(`spotnav_rate_limited`), and a body of at most 16 KiB as JSON (`spotnav_body_too_large`). The charger lookup's codes and `spotnav_unsupported_api_version` as for the other
commands.

The answer:

```json
{
  "api_version": 1,
  "ok": true,
  "error": null,
  "preview": {
    "state": "proposal_ready",
    "reason": "ready",
    "plan": {
      "periods": [{"start": "2026-09-22T13:00:00+02:00", "end": "2026-09-22T15:15:00+02:00"}],
      "planned_kwh": 15.5,
      "requested_kwh": 15.5,
      "power_kw": 6.9,
      "cost": {"value": 18.4, "currency": "SEK"},
      "finish": "2026-09-22T15:15:00+02:00",
      "unpriced": false,
      "short_of_deadline": false
    },
    "status": {"tone": "normal", "lines": []},
    "strip": {
      "start": "2026-09-22T06:00:00+00:00",
      "end": "2026-09-22T16:00:00+00:00",
      "quarters": [{"start": "2026-09-22T06:00:00+00:00", "price": 98.2, "planned": false}]
    }
  }
}
```

- `state` and `reason` are the planning state and reason the dashboard's `planning` block would carry after the
  Save.
- `plan` is `null` when no plan would be made; `status` then says why, in the dashboard's [status
  codes](#the-dashboard-document) (`settings_incomplete`, `waiting_for_publication`, `planning_unavailable`, ...). `periods`
  are the periods as shown (the last ending where a need is expected to be met), `finish` the last one's end,
  `cost` in the major unit (`null` for an unpriced plan), `short_of_deadline` a best-effort plan that does not make
  the departure (its `departure_shortfall` line is in `status`).
- `status` holds the plan's own lines only (a shortfall, a capped or estimated need, unpriced hours, a wait for
  prices). What the charger is doing now (charging, a pause, the site) is the dashboard's.
- `strip` is the hours the plan was chosen from, from the first usable quarter to the departure (or the end of
  its horizon), each quarter with its price in the minor unit with the fees the plan priced it with, and whether it
  is planned. `null` without a plan; `quarters` is empty for a plan made without prices.
- A body a Save would refuse answers `ok: false`, `error` the Save's own code (`invalid_amps`,
  `invalid_departure`, ...) and `preview: null`.

## Home Assistant events

Each charger has one **Charger events** entity (an `event` entity). It fires:

| `event_type` | When | Attributes |
|---|---|---|
| `charge_started` | the charger starts charging | `energy_register_kwh` |
| `charge_finished` | the charger stops charging | `unplugged`, `energy_kwh` the charger's energy register counted since the start (when it has one), and the charge's cost: `cost`, `currency`, `average_price`, `unpriced_kwh` (below) |
| `plugged_in` / `unplugged` | the vehicle is connected / disconnected, for a charger that can say | none |
| `plan_installed` | a charging plan different from the one before is installed, by Auto or by hand | `start`, `end`, `periods`, `amps`, `energy_kwh`, `automatic` |
| `plan_at_risk` | the departure cannot be met with the energy that remains (the plan is the best effort, or not one whole quarter-hour is left); once each time it becomes so | `departure_time`, `requested_kwh` |

Starting Home Assistant is not an event, and neither is a charger integration that reloads: while its charging state reads unavailable or unknown nothing is observed, and the charge is as it was last seen.

**What a finished charge cost.** `charge_finished` prices the energy delivered since the charge started from its
[charge session](card.md#charge-history), as the session sensors do: the spot price of each interval plus the
energy tax and grid fee you enabled, times VAT, with your current settings.

| Attribute | Meaning |
|---|---|
| `cost` | What the priced energy cost, in the major unit (kronor, euro), as the *Last charge cost* sensor rounds it. |
| `currency` | The ISO code of that cost (`SEK`, `EUR`, …). |
| `average_price` | `cost` per priced kWh, in the major unit. |
| `unpriced_kwh` | The charge's energy that had no published price, counted but not in `cost`; `0.0` when all of it was priced. |

When none of the energy could be priced (no published price, or the price settings cannot be resolved), `cost`,
`currency` and `average_price` are `null`, never `0`. Without a session record (none was kept) all four are `null`.
A pause shorter than two minutes continues the session; the event
after it covers only the energy since the charge started again, so two events add up to the session. The figures
are those the session holds when the charge stops: a reading the charger sends after the stop goes into the
session, and so into the *Last charge* sensors, but not into the event that has already fired. Example, a
notification when a charge ends:

```yaml
automation:
  - alias: Car charged
    triggers:
      - trigger: state
        entity_id: event.my_charger_charger_events
        attribute: event_type
        to: charge_finished
    actions:
      - action: notify.mobile_app_phone
        data:
          message: >
            {%- set a = trigger.to_state.attributes -%}
            Charging finished:
            {{ '%.1f' | format(a.energy_kwh) if a.energy_kwh is defined else '?' }} kWh
            {%- if a.cost is not none %} for {{ '%.2f' | format(a.cost) }} {{ a.currency }},
            average {{ '%.2f' | format(a.average_price) }} {{ a.currency }}/kWh{% endif %}.
```

This says, for example, "Charging finished: 12.4 kWh for 18.60 SEK, average 1.50 SEK/kWh".

## The dashboard document

`dashboard` returns one JSON document, enough for a client to render a charger without further
reads. Its main blocks: `charger` (identity, availability and `capabilities`), `chargers`,
`settings` (the canonical record with `revision`), `fiscal`, `market`, `prices` (the price
intervals), `plan` and `planning` (proposal, installed plan, and why), `control` (the immediate
action and the automatic action, with pause choices), `live`, `status` (typed status lines), `strategy`
and `strategy_state`, `vehicles` and `soc`, `site`, `charging_phases`, `phase_detection`, `charge_progress`, `progress`,
and, where the charger prices with an electricity price, `price_source` and `grid_tariff`.
Example documents are in `tests/fixtures/dashboard/`.

**Plans told.** The additive root `plan_notice` (`{"seq", "at"}`) counts the new plans Home Assistant told
about: `seq` rises by one each time the charger's notifier decides a new plan is news (the decision that sends
the Companion phones' "New plan" and wakes the paired app, see [Notifications](notifications.md#when-a-new-plan-is-told)),
whether or not a phone was chosen or a limit held the message back; `at` is when that was (ISO), `null` before
the first. It is kept across restarts and never falls back. A client that tells a person about new plans does so
when `seq` changes, instead of comparing plans itself; a backend without the field leaves that to the client.
`null` for a charger without a notifier.

**A person's own pause.** A Start or Stop pauses automatic execution for the plug-in session: the
`control.pause` record then carries the choice `manual` with two more keys, `action` (`start` or `stop`)
and `scope` (`plug_in`, the plug-in the car is in, or `next_plug_in` for a Stop given with no car), and no
`expires_at`. `manual` is never one of the `pause_choices` and cannot be asked for; the automatic action
beside it is `resume` (a `stop` with another choice replaces it). The `paused` status line carries
`action` and `ends` (`unplug`, `next_plug_in`, or `resume` on a charger that cannot tell when a car is
plugged in); both are null for every other pause. Every other pause record keeps its three keys.

**A Start or Stop on its way.** After a person's Start, until the charger reports charging, and after a
person's Stop, until it reports it has stopped (or, for either, at most 30 seconds), `control` offers
nothing on either axis: `immediate_action` and `automatic_action` are `none` with the reason
`action_pending` (an automatic axis already showing a pause the person chose for a span keeps it). A client
shows *Starting…* or *Stopping…* meanwhile. Only the offer waits: the command itself was sent at once, a
`stop` is accepted (and sent) whatever is pending, and a `resume` of the pause a pending Stop took is accepted
too and ends the wait, as Follow does. When the 30 seconds pass without the report the axes answer from
the charger's state again: Stop is offered again while it still charges; an unanswered Start is also
reported as `action_failed`. A Stop that failed is never pending (`pause_stop_failed`, Stop again).

**Battery room.** The `soc` block's additive `room_kwh` is the wall energy the battery still has room for,
to the car's own charge limit (else 100 %): `capacity_kwh x (ceiling - value) / 100 / 0.9`, `null`
without a level or a battery size. A manual amount is planned at most that much; when it is capped the
status carries `need_limited_by_room` (`kwh`), and while a Start is in effect for a plan the car ends
itself (a capped amount, or a target at or above the car's own limit) it carries
`charging_to_vehicle_limit` (`percent`, the car's limit, 100 when it states none). A client shows its kWh
slider up to `room_kwh` when it is present; an older backend has neither field nor line. Under "Fill"
(`fill_to_limit`) the status carries `filling_to_limit` (`kwh`, the room now) in place of
`need_limited_by_room`, and, while no room is known, the notice `fill_room_unknown` (`kwh`, the stored amount
planned instead).

**A learned battery size.** When no battery size comes from a setting, the car's integration or its profile, the
size learned from the car's charges at this charger stands in for it: the learned wall kWh per percent x 100 x 0.9
(battery side), used once one ended charge raised the level at least 10 points with at least 5 kWh and only between
10 and 200 kWh. The `soc` block's `capacity_kwh` (and so `room_kwh` and `need_kwh`) then carries it, the status
carries the normal fact `battery_size_learned` (`kwh`, whole kWh) whenever the plan's need was worked out with it
(a target, "Fill" or an amount capped at the room), and a vehicle row carries the additive `learned_capacity_kwh`
(one decimal, `null` when a size is stated or nothing is learned): a hint for the battery size setting, never its
value; the row's `capacity_kwh` and `capacity_source` stay `null`. The card's slider goes past the room to `min(capacity to the car's limit, max(30 kWh,
2 x room))`, rounded up to its half-kWh step, with a "full" mark at the room; its last step is "Fill".

**A target's need.** The `soc` block's `need_kwh` is the wall energy the target needs, the same figure the plan
buys: `(min(target, floor(vehicle_max_percent)) - value) x capacity_kwh / 100 / efficiency`. `efficiency` is 0.9
until the car's wall energy per percent is learned and trusted (see [strategies](strategies.md)); from then it is
the efficiency that gives the learned figure with `capacity_kwh` (between 0.70 and 1.0), so a client that works
the need out itself for a moved target slider gets the plan's figure. With a learned figure but no battery size,
`need_kwh` is given and `efficiency` stays 0.9; a client then has no formula for another target. The card's kWh
slider top, which divides by `efficiency`, follows the learned efficiency too; `room_kwh` keeps 0.9, as the
planner's cap on a manual amount does, so the two stay equal.

**Waiting for the car's new level.** With a target, the planning state `nothing_to_charge` has the reason
`waiting_for_vehicle_update` when the charge that just ended delivered, by measurement, at least what the
car's last reading needed and the car has not reported since, or when the car ended the charge by itself while
the estimate from delivered energy says it is full (a charge control still on or a period still ahead does not
keep this off once the car ended the charge); the status headline is then
`waiting_for_vehicle_update` (no params) in place of `nothing_to_charge`. A client that does not know the
code shows it as any unknown code.

**The car ended the plan's charge short of the target.** When the car ended the plan's charge by itself on a level
nobody read then (the estimate from delivered energy, or a reading from before the end) below the target, the plan
waits for the car's level: no window starts the car again until a reading made after the end, and the planning state
is `nothing_to_charge` with the reason `waiting_for_vehicle_update` (no departure ends this wait). The status
headline is `car_ended_waiting` (tone `normal`; `soc_percent`, the level the car ended at, `null` when none was
known; `estimated`, a boolean: whether that level is the estimate) in place of `charging_now` (the window open at the
end may keep its charge control on), the plan's lines and `waiting_for_vehicle_update`, while the car draws nothing.
The webhook says `waiting_for_vehicle_update` (no params) in its place unless the request asks for it
(`"reads": ["car_ended_waiting"]`).

**Best effort before a departure.** When the need cannot be met by the departure, the plan is every
whole quarter-hour from the first usable one up to the departure (one run, automatic or whatever number `max_periods` says,
so the period limit never costs energy), installed and charged like any other: `planning` stays
`proposal_ready` (or `proposal_unpriced`) and the status carries the notice `departure_shortfall` (`kwh`,
the planned energy; `requested_kwh`, the need; `soc_percent`, what a target reaches by the departure,
null for a manual amount; `departure`, the instant). The card shows the percent as a whole number rounded
down. Only a departure with not one whole quarter-hour left is still refused, as `planning_unavailable`
with the reason `deadline_too_short`. An older client that does not know the code refuses the document.

**Top-off.** When the plan's last window ends with the car still drawing on such a charge, the charge stays
on until the car stops by itself (two minutes without current), at most an hour past the window and never
past the departure. Meanwhile the status headline is `topping_off` (`until`, the latest instant it may run
to; a fact line after `charging_to_vehicle_limit` under a solar or hybrid headline), `charging_to_vehicle_limit`
stays, and the execution state is `active`. A charge the car ended this way is notified as *Charging
complete: the car is full*; one that reached the hour ends as the window's end would have.

The additive `charger_priority` block is this charger's place in its site's order, which capacity
allocation and solar surplus both follow: `null` for a charger on no site, else `{"value": "first" |
"normal" | "last", "choices": ["first", "normal", "last"], "writable": bool}`. `writable` is true over
the webhook (`update_charger_priority`) and for an administrator's socket (`spotnav/update_entity_config`),
false for a reader. An older backend has no block; a client then shows no priority.

**Price areas in the contract-v2 shape.** `market` carries three additive fields: `market_timezone` (the
zone whose calendar day one day file covers; equal to `timezone` except for Great Britain,
`Europe/Paris` beside `Europe/London`, and Portugal, `Europe/Madrid` beside `Europe/Lisbon`), `included`
(the names `vat`, `tax`, `grid_fee`, of what the published price already contains) and `source`
(`{"name", "url"}`, where the prices come from: the area's provider, or `null` with none). Each `fiscal` component of an included part has
the policy `included`, with no effective value. `spotnav/get_market_options` gives every area the same
`market_timezone` and `included` in the settings' own component names, and `source` `null` since 2.0 (the list
names no source; the dashboard's `market.source` does). Days in `prices` are
always days in `timezone`; a Great Britain day is cut from the two day files that cover it.

**Phases.** A charge uses the smaller of the charger's wiring (1 or 3: the site's phase wiring for the
charger, or, for a charger in no site, its own `charger_phases` field in `spotnav/get_entity_config`
and `spotnav/update_entity_config`, `"1"` or `"3"`) and the planned vehicle's onboard charger. A
vehicle row in `vehicles` carries `onboard_phases` (1 or 3, three until told), written with
`update_vehicle` as `changes: {"onboard_phases": 1}` (`null` returns it to three; any other value is
`invalid_onboard_phases`). The additive root block `charging_phases` states the result:
`{"phases": 1|3, "charger": 1|3, "vehicle": 1|3|null, "limited_by": "vehicle"|null}`; `limited_by` is
`"vehicle"` when the car, not the wiring, sets it. The settings record's `phases` is no longer a
setting: it is read-only in practice and carries the effective count for older clients. A
replacement may still send it, or leave it out, and either is accepted and ignored (a value other than
1, 3 or `null` is still `invalid_phases`). `proposal.phases` and `installed.phases` are the effective
count too.

For a charger in a site, `get_entity_config` also lists `charger_phases` with `"writable": false` and the
site's wiring as its `value`; a write to it is `not_writable`. SpotNav learns from charges: while a charge
draws more than 1 A, the phases carrying at least 2 A are counted on the charger's measured current per
phase (its own three current entities, else its site's measurement of it). Three phases confirm the
wiring and the car; a charger in no site whose wiring nothing says gets three by itself (logged); a
one-phase charge on three-phase wiring, twice running for the same planned vehicle that has no onboard
answer yet, sets the additive `suggested_onboard_phases` (`1` or `null`) on that vehicle's row in
`vehicles` and in the `update_vehicle` answer. Nothing is changed by the suggestion: the client asks and
answers with `update_vehicle`, `1` to accept and `3` to keep three phases (either answer ends the
question; a three-phase charge starts the count over).

**Why solar has no full basis.** Additive status codes name it. In place of `solar_no_reading_waiting` /
`solar_no_reading_stopped`, a solar charger that is off for want of a basis says `solar_no_grid_power`
(`entity`: `null` when a direct site's total grid power is not set, else its entity that has no fresh
reading) or `solar_battery_unreadable` (`entity`); while solar runs, the same line follows the headline.
`solar_charger_current_missing` (`entity`: `null` when not set, else the unreadable entity) follows the
headline while the charger's own measured current is unknown and solar runs blind at the minimum current
(the unreadable case only while the charge is on or a start is pending: a charger that is not charging
reports no current by nature),
`solar_charger_current_missing` also carries the additive `integration` and `sensors`: when the current is not
set and the charger's own integration has sensors for it, the integration's name and the sensors named for a
sentence (`"Peblar"`, `"Current phase 1–3"`), else both `null`;
and `solar_site_incomplete` (`phases`) while a direct site's phase measurement is unusable and solar runs
on the total grid power only. Every line naming an entity also carries the additive `entity_name` (its friendly name, the id when it has
none; `entity_names`, parallel to `entities`, on `site_meter_unavailable`), which clients prefer to the id.
`site_meter_unavailable` (`entities`, `cause`: `inverter_standby` or
`meter_unavailable`) replaces `site_measurement_problem` when the phases without a value are the meter's
sensors reading `unavailable`/`unknown` together: every phase at once, or a known inverter integration's
(SolaX, Huawei, SolarEdge, Fronius, ...), which go unavailable when the inverter is in standby at night.
The site sensor's `solar_surplus` rows carry the same facts as `basis_problem`, `basis_entity`,
`charger_current`, `charger_current_entity` and `site_incomplete_phases`.

**A solar charger with no car.** In place of `solar_waiting_for_sun`, a solar charger whose charger says no car
is plugged in says `solar_no_car_surplus` (`surplus_kw`: the surplus the sun's rules reckon for it, in kW, `null`
when not known; the card and the app show it with one decimal, "Solar · surplus available (4.2 kW)") while that surplus covers the start minimum a charge would start at, and
`solar_no_car` otherwise or while solar has no full basis (both tone `normal`, no notification). Hybrid shows its
plan's line on an empty charger, as before. A released SpotNav app words a code it does not know as "see Home
Assistant", so the webhook says `solar_waiting_for_sun` in place of either line unless the request asks for them
(`"reads": ["solar_no_car_status"]`). The `solar_surplus` rows in the site's diagnostics carry the additive
`start_a`.

**The site's sensors.** The site state sensor (*Capacity state*) keeps its states; the healthy one, `observing`,
is named **Measuring**. Its additive attribute `site_activity` says what the site is doing in a word: `measuring`
(healthy, load balancing off), `balancing` (healthy, load balancing on) or `not_measuring` (any other
state); the site's diagnostics carry the same. A charger's allocation (`state` on its *Proposed current* sensor and
in the diagnostics' `result`) has the additive `not_requesting` (reason `no_current_requested`, no limiting phase)
for a charger that asks for no current, which used to read `below_minimum_current`; a reader that does not know it
treats it as any unknown state. Every site has the sensor *Solar surplus* (W, `null`/unknown without a usable basis)
with the attributes `export_w`, `battery_w`, `car_w` and `priority`; see [Solar](strategies.md#solar).

After 1.14.3 the *Capacity state* and *Proposed current* sensors no longer carry what changes with each meter
reading, and are written only when what they show changes (each changed attribute was a row in the recorder).
Removed from *Capacity state*: `max_age_s`, `measured_site_current_a`, `phase_age_s`, `phase_report_age_s`,
`phase_liveness`, `phase_signed_active_power_w`, `phase_signed_active_power_age_s`,
`phase_signed_active_power_reason`, `phase_current_basis`, `estimated_power_factor`, the four headroom figures,
`battery_yield_basis`, `limiting_phase`, `site_measurement_configured`, `charger_control_available`,
`current_control_available`, `calculation_available`, `has_unknown_active_request`, `grid_power`,
`yield_stepping`, `battery_probe`, `balancing_resume`, `solar_surplus` and `hybrid`; from *Proposed current*:
`measured_charger_current_a`, `limiting_phase` and `regulator_limiting_phase`. Every one of them is in the site's
diagnostics (`result`, `capability`, `grid_power`, `regulator` and the per-charger blocks). The currents kept
are rounded to 0.1 A. See [Troubleshooting](troubleshooting.md#site).

A site's `warnings` in `get_entity_config` carry the additive `unavailable_entities` (a list) and
`inverter` (bool): for `measurement_unhealthy`, the meter's sensors unavailable together as above; empty
and false otherwise.

A site's `measurement` in `get_entity_config` carries the additive `current_source`: `null`, or the source
a direct site reads its phase currents from in place of the three `direct_L{n}` entities (set by a
detection, e.g. an Easee Equalizer, or the setup wizard), read-only:
`{"kind": "attributes", "entity_id", "name", "attributes": {"L1", "L2", "L3"}, "entity_ids": null}` for one
entity carrying each phase as an attribute (`name` is its friendly name), or
`{"kind": "separate_entities", "entity_id": null, "name": null, "attributes": null, "entity_ids": {"L1", "L2", "L3"}}`.
While it is set the `direct_L{n}` fields report no `current`, and a write that leaves them out keeps it.
A write that names any of them replaces it, so all three are then required (`required` on each missing one).

A site's `detection` in `get_entity_config` carries the additive `chargers`: one row per member charger whose
wiring reads no measured current and whose own integration has the sensors for it,
`{"id", "charger_name", "integration", "sensors", "entity_ids", "disabled_entities"}` (`sensors` named for a
sentence, `disabled_entities` the ones the integration ships disabled). `apply_detection` takes its `id`
(`charger_current:<charger id>`) as it takes a meter's: the source goes into that charger's wiring and the
entities the integration disabled are enabled, never one a person disabled. A charger's
`measured_current_source` field carries the additive `own_sensors`: `{"integration", "sensors"}` while the
wiring reads none and such sensors exist, else `null`.

A charger's `energy_register_entity` field in `get_entity_config` carries the additive `none`, as
`current_limit` does: `{"allowed": true, "chosen": bool, "automatic": {"entity_id", "friendly_name"} | null}`,
`automatic` being the lifetime register detection finds now, also while "None" is chosen.
`update_entity_config` takes `"none"` for it (no register, and none looked up or detected again) and `""`
for the one SpotNav finds itself; while "None" is chosen the field reads back as `"none"`.

Since 2.3 a site's fields in `get_entity_config` carry `import_target_kw` after `safety_margin_a`: a number field
(`required: false`, `minimum: 0.1`, `value` in kW or `null` for none), the site's import target. `update_entity_config`
takes a number up to 1000, or `null` or `""` to clear it, and writes it without reloading the site; any other value
is `invalid_value` on that field.

A site's `warnings` in `get_entity_config` carry the additive `limits_a` (`null`, or for
`battery_import_limit_differs` `{"battery": A, "spotnav": A}` per phase). That warning says a home battery
integration's own grid import limit and SpotNav's (the main fuse minus the safety margin) differ by more
than 1 A per phase; it is raised for the Sigenergy integration's `Grid Import Limitation` number when it is
enabled and holds a real value (4294967.295 kW means no limit).

The additive `connection` block, `{state, source}`, says what the charger itself reports about the vehicle
and the charge, in one vocabulary: `disconnected` (no vehicle), `connected` (a vehicle, nothing charging yet),
`charging`, `paused` (a vehicle is connected and the charge is suspended by the charger, the vehicle or
SpotNav), `finished` (the vehicle stopped, full or at its target, and is still plugged in), `error` and
`unknown`. `source` is the entity the state was read from, or `null`. Every raw value is mapped explicitly
per charger integration (an OCPP connector's status, Easee's `status`, Zaptec's `charger_operation_mode`,
Wallbox's `status_description`, and the others with a status sensor); a value not listed is `unknown`, never
a guess, and a charger that is only a switch is `charging` while the switch is on and `unknown` otherwise.
A client shows nothing for `unknown` and ignores a state it does not know.

The additive `progress` block says how far a running charge has come and when it ends, decided once in Home
Assistant so the card and the app draw the same bar; `null` while no charge runs (`live.charging` false),
while `starting_up` is on, and for a `connection` of `disconnected`, `finished` or `error`:

    {"basis": "target" | "energy" | "vehicle_limit" | "open", "percent": 0-100 | null,
     "ends_at": ISO | null, "power_kw": kW | null, "power_source": "measured" | "planned" | null,
     "moving": bool, "start_soc_percent": % | null, "started_at": ISO | null, "delivered_kwh": kWh | null}

The plan drives the charge when an installed period holds now and no pause stands; then the driver counts:
`target` is the car's level as a share of the target (rounded, never above the car's own limit), the end
from `soc.need_kwh`; Fill is `vehicle_limit`, the level as a share of the car's limit (else 100 %), the end
from `soc.room_kwh`; a fixed amount is `energy`, `plan.delivered_kwh` over the whole amount (`delivered_kwh +
remaining_kwh`, else the requested amount), with no block without `delivered_kwh`. Any other running charge
(a person's Start, a charge under a scheduled pause, one the charger began itself, a solar charge outside
the periods) counts to the car's own limit as `vehicle_limit`, its end from the battery's room; with no
level it is `open` (no `percent`, no `ends_at`). `percent` is rounded down. `ends_at` (UTC) is laid along
the installed periods while the plan drives and never falls after the last one, else a straight line from
now; it is `null` for solar and hybrid unless a person started the charge, while the charge stands still,
with nothing left, or with no power known. `moving` is true while current flows: the connection is
`charging` (or `unknown`), the car is not seen asking for no current, and a measured current or power is
not near zero. `power_kw` (only while moving) is measured when the charger measures it (`power_source:
measured`), else what SpotNav assigned now, the installed schedule's power or current, the proposal's power
or the settings' current (`planned`). `start_soc_percent`, `started_at` and `delivered_kwh` come from the
open charge session: the car's level when it opened, when that was, and the energy measured since (`null`
for a session whose energy is an estimate). An older backend has no block; a client then shows none.

`live.measured_current_a` is the current the charger draws now (its highest phase), `null` where nothing
measures it: an OCPP connector's current import; the measured-current sensors of Easee (`current`), Zaptec,
go-e (API v2 and MQTT), Peblar, NRGkick, Charge Amps, Lektrico and the Tesla Wall Connector when the charger
was set up with them; else, for a charger in a site, the per-phase measurement the site reads for it (an
Easee's terminal attributes, or any three current entities). A charger behind a smart plug has no current,
but its power sensor is the measured `power_kw`. Wallbox, Ohme, myenergi, Alfen, ABB, SmartEVSE, Webasto
Next and the others with no current sensor in their integration have only the planned power.

The additive `starting_up` block, `{active, until, waiting_for}`, says the integration loaded a moment ago and
a source is still awaited: a configured solar forecast that has not loaded (`forecast`, only for the hybrid
strategy) or a charger whose status sensor and charge control still read unavailable or unknown (`charger`).
It is on for at most three minutes after the integration loads and ends as soon as the sources report;
`until` is that cap while it is on and `null` otherwise. While it is on, `status` is the single line
`starting_up`, `live.charging` keeps its boolean (read it together with `starting_up`), and a
hybrid `strategy_state` says `unknown` with reason `starting_up` instead of "no forecast". A client shows
"Starting up…" and offers no Start or Stop meanwhile; an older backend has no block, and a client then
behaves as before.

The additive `sessions_summary` block holds this month's and last month's charge sessions
(`sessions`, `energy_kwh`, `cost`, `currency`, `average_price_minor_per_kwh`, `solar_share`,
`savings`). Cost is in the major unit, prices in the minor unit per kWh; `savings` compares with
the day's average price and is an estimate (`savings_estimate: true`). A client ignores keys it does
not know. Example answers of `spotnav/get_sessions` are in `tests/fixtures/sessions/`.

### Vehicle identification

At a charger more than one vehicle can charge at, SpotNav finds out which one was plugged in
([how](vehicle-identification.md)). The additive root block `identification` is `null` while no car is plugged in or
fewer than two cars can charge here; otherwise it is present, also with identification off or after a restart
(then `state` is `decided`, `method` what stands, `since` `null` and `evidence` empty):

```json
{"state": "asking", "method": "assumed", "vehicle_id": "<the target vehicle now>",
 "since": "2026-10-06T17:00:00+00:00",
 "candidates": [{"vehicle_id": "...", "name": "Kia", "likely": false}, {"vehicle_id": "...", "name": "Tesla", "likely": true}]}
```

`state` is `waiting` (looking at the cars' own reports), `asking` (the question is open: a client shows it with
one button per candidate, in the given order) or `decided`. `method` is how the car was decided so far:
`plug_sensor`, `location`, `answered`, `manual`, `camera` (the charger's camera recognised it), or `assumed`
(nothing decided it yet, or nobody answered); a client shows a method it does not know as none.
While `state` is `waiting` the status block leads with the line `identifying_vehicle`, and while it is `asking` with
`asking_vehicle` (both tone `normal`, no params), ahead of every other line of a block that is not blocking and not
`starting_up`; neither is sent once the car is decided. The card ships with the integration and knows both. A
released SpotNav app words a code it does not know as "see Home Assistant", so the webhook leaves both lines out of
its status block unless the request asks for them (`"reads": ["identification_status"]`), the way the settings
fields above are withheld.
`likely` marks a car whose own plug sensor says it was plugged in. The additive `evidence` lists, per car, what it was judged by at the
last look: `{"vehicle_id", "plug": {"entity_id", "state", "changed", "reported"} | null, "location": {"entity_id",
"home", "reported"} | null, "verdict": "plugged_in" | "likely" | "not_plugged_in" | "away" | "elsewhere" | null}`
(the diagnostics carry the same). When the charger's camera was asked, the list ends with one entry without a
`vehicle_id`: `{"camera": {"entity_id", "answer": <vehicle id> | "none" | null, "confidence": "high" | "medium" |
"low" | null, "used": <bool>}}` (`answer` `null`: the camera or the model failed or took too long; `used`: the answer
decided the car or ordered the candidates). A client that reads the list by `vehicle_id` skips it. A vehicle row in `vehicles` carries the
additive `identification`: `{"plug": source, "location": source}`, each source `{"entity_id", "name", "chosen",
"candidates": [{"entity_id", "name", "state"}]}`; `entity_id` `null` with `chosen` false is nothing found, or
several to choose from, and with `chosen` true it is the person's "none". `state` is the entity's current state
text (`null`: no state). The `plug` source also carries `others` (same shape as `candidates`): the car's other
entities a person may choose as the plug source (any `binary_sensor`, or a `sensor` without a unit and with no
device class or `enum`; not those already in `candidates`; sorted by name), and `reading`: what the source
says now, `"plugged"`, `"unplugged"` or `"nothing"` (its value does not read as either), `null` when there is no
source. A client that ignores keys it does not know can leave all three out.

`spotnav/identify_vehicle` (any signed-in user: the question goes to the household's phones) and the webhook action
`identify_vehicle` take `vehicle_id`, one of this charger's cars (`vehicle_ids`, every detected car when `null`) or
`"guest"` (a [guest car](#the-guest-car), at any charger), and
answer `{"api_version": 1, "ok", "error", "identification"}` with the block after the answer. They are taken whenever
a car is plugged in: an answer while the question is open, or a correction of a car already decided (also where
nothing was identified: identification off, one car, a decision from before a restart). Either way it is recorded
as `answered` and holds like an answer for the car's stay (a replug within two minutes keeps it, unless the
answered car itself reports unplugged or away), wins over every automatic result, replaces an open question on
every phone silently, and writes the settings as any vehicle choice does. Refusals: `spotnav_not_identifying` (no
car is plugged in), `spotnav_invalid_value` (not one of this charger's cars), `spotnav_unknown_charger`,
`spotnav_unsupported_api_version`; over the webhook HTTP 400 with the same code. The settings that steer
identification (`vehicle_ids`, `identify_mode`, a car's sources) stay an administrator's.

`spotnav/choose_vehicle_identification` (administrators) and the webhook action of the same name take
`vehicle_id`, `source` (`plug` or `location`) and `entity_id`: one of the source's `candidates` (for `plug` also
one of its `others`), `"none"` (the car
has no such source) or `null` (back to automatic). The answer carries `identification`, the vehicle row's block
after the write; a value that is not one of these is `spotnav_invalid_value`.

The phones' question is the notification event `vehicle_identify`: tag `spotnav_<charger id>_identify`, the
message "Which car is plugged in?" in Home Assistant's language, and `data.actions` with one
`{"action": "SPOTNAV_ID_<charger id>_<nonce>_<index>", "title": "<car name>"}` per answer: each car and "Guest car" (in
Home Assistant's language) with one or two cars; with three or more, the likeliest car, "Guest car" and
`{"action": "URI", "title": "Open SpotNav", "uri": <url>}`. "Guest car" is first when the dashboard's `guest.first`
says so (every car ruled out, or the camera saw none of them), else last. At a charger with one car the question is
asked only when that car is ruled out, and its `identification` block is then `asking` with one candidate. The nonce is 128 random bits, new for
each question; a `mobile_app_notification_action` event with a matching action is the answer, and the first one
wins. A retired question is replaced on every phone by the same tag without actions, silently (`alert_once: true`,
`push: {"interruption-level": "passive"}`); unplugging sends `clear_notification`. A button of a question that is gone (answered, unplugged, a restart) takes the question off the phones.
Each session in the history carries the additive `vehicle_decided_by` (`null` where nothing decided it).
The charger's diagnostics (and the debug bundle) carry `vehicle_identification.history`, the last ten plug-ins with
their evidence, camera query, question, answer and corrections ([fields](vehicle-identification.md#for-field-reports)).
It is a support aid, not an API: its fields may change.

#### The guest car

A visitor's car is planned by kWh with no vehicle, once a person answers `identify_vehicle` with `"guest"`
([how it works](vehicle-identification.md#a-guest-car)). While a guest plan stands, the settings record's
`target.vehicle_id` is `"guest"`, `driver` `manual_kwh`, `requested_kwh` the guest's amount and `fill_to_limit`
false; `target_vehicle_id`, the `soc` block's car and the vehicle facts are none. A settings write that names
`"guest"` while no guest plan stands is refused (`invalid_target`: a guest is chosen by the answer), and one that
names no vehicle during a guest plan keeps the guest. Changing `requested_kwh` during a guest plan is the guest
amount the next guest starts from too. The root block `guest` (after `setup`) is:

```json
{"active": true, "amount_kwh": 10.0, "first": false}
```

`active`: this charger plans for a guest car now; `amount_kwh`: the guest amount (1 to 150 kWh, `10` until someone
changes it, one for the whole installation, the last change wins); `first`: "Guest car" goes first in the open
question. The status lines are `guest_planned` (`kwh`, `departure`: the departure the guest's plan is for, `null`
without one), ahead of the plan's headline, and, once the amount is delivered, `guest_done` (no params) as the
headline: the charger is held until the guest is unplugged, and a person's Start still charges.

`spotnav/update_guest` (administrators) and the webhook action `update_guest` take `amount_kwh` and answer
`{"api_version": 1, "ok", "error", "guest"}` with the block after the write; `spotnav_invalid_value` for an amount
out of range.

The released SpotNav app knows none of this, so the webhook sends it only to an app that asks for it
(`"reads": ["guest"]`): without it the `guest` block and the two status lines are left out, an `identification` block
with fewer than two candidates (a one-car charger's question) is `null`, and `"guest"` reads as `null` in the
settings' `target.vehicle_id` and the `identification` block's `vehicle_id`.

#### The camera

The additive root block `camera_identification` is present only where Home Assistant has a camera and an AI Task
entity that takes attachments, or a camera is chosen (`settings.identify_camera`); otherwise the key is absent:

```json
{"cameras": [{"entity_id": "camera.norr", "name": "Norr"}],
 "ai_tasks": [{"entity_id": "ai_task.ollama", "name": "Ollama AI Task", "model": "qwen3-vl:4b-instruct"}],
 "references": {"<vehicle id>": [{"kind": "day", "taken_at": "2026-10-07T12:00:00+00:00", "colour": true}]}}
```

An AI Task entry's `model` is the model its config subentry names (`null` when it names none): several entities
often share one name, and a client tells them apart by it, or else by the entity id. `references` lists, for each
of this charger's cars, its reference pictures from the chosen camera (`day` first;
`colour` says whether the picture has a colour signature, which a night or infrared picture has not); never the
pictures themselves. A reference picture is kept whole and cropped with the frame drawn now whenever it is compared
or shown, so drawing the frame again never makes it useless. (A `stale` field an earlier build of this branch sent
is gone: absent means usable.) The camera and the AI Task entity are chosen with a settings replacement
(`identify_camera`); the frame and the pictures with these commands, each an administrator's over the WebSocket
(`spotnav/<name>`, `api_version: 1`, `charger_id`) and the paired app's over the webhook (the action `<name>`, bound to
its charger). Every answer is `{"api_version": 1, "ok", "error", ...}`; a picture is `{"content_type": "image/jpeg",
"data": <base64>, "width", "height"}`. The client does no image processing: Home Assistant crops and scales.

| Name | Takes | Answers |
| --- | --- | --- |
| `camera_snapshot` | `camera_entity_id` (optional, WebSocket only; default the chosen camera) | `picture`: the camera's whole picture now, to draw the frame on |
| `save_camera_frame` | `frame`: `{x, y, w, h}` or `null` | `identify_camera`: the settings' camera after the write (nothing else in the settings changes) |
| `take_reference_picture` | `vehicle_id` (one of this charger's cars), `kind`: `day` or `night` | `vehicle_id`, `references`: that car's pictures after it |
| `delete_reference_picture` | `vehicle_id`, `kind`: `day`, `night` or `null` (all of the car's) | `vehicle_id`, `references` |
| `reference_picture` | `vehicle_id`, `kind` | `vehicle_id`, `kind`, `picture`: a thumbnail (at most 240 px) |

Refusals: `spotnav_no_camera` (no camera chosen, or it is not there), `spotnav_no_picture` (the camera gave none),
`spotnav_invalid_value` (not one of this charger's cars, an unknown kind or a bad frame, no such picture),
`spotnav_not_admin` (WebSocket), `spotnav_unknown_charger`, `spotnav_unsupported_api_version`; over the webhook HTTP
400 with the same code. A reference picture is the camera's whole picture (at most 1600 px on its long side); its
thumbnail and what the model is shown are cropped with the frame drawn now.

**The paired app sees only the chosen camera.** Over the webhook `camera_snapshot` takes no `camera_entity_id` (one
given is `spotnav_invalid_value`) and answers the chosen camera's picture (`spotnav_no_camera` with none chosen),
and a `settings` replacement may echo `identify_camera` and move its `frame`, but a different `camera_entity_id` or
`ai_task_entity_id`, or `null` while a camera is chosen (or a camera while none is), is refused with
`invalid_camera`: choosing the camera and the AI Task entity, where the pictures go, is an administrator's.

At a plug-in the camera is asked through `ai_task.generate_data` with a structured answer (`vehicle`: `car_1` …
`car_n` or `none`; `confidence`: `high`, `medium` or `low`) and the pictures as attachments:
the crop of the picture now first (picture 1), then each candidate's reference pictures. The attachments are `media-source://spotnav/<token>` ids that
SpotNav's media source resolves to a local file only while that one call runs (the crop is a temporary file,
deleted after the call); browsing SpotNav's media source shows nothing.

### History by month

`spotnav/get_sessions` (and the webhook action `sessions`, with the same fields) takes an optional
`month`, `"YYYY-MM"`, default the current month, at most 24 months back and never in the future (anything
else is `spotnav_invalid_range`). The answer keeps every earlier field and adds:

| Field | Meaning |
| --- | --- |
| `month` | The month the figures below are for. |
| `month_summary` | That month's totals: `sessions`, `energy_kwh`, `cost`, `average_price_minor_per_kwh`, `solar_share`, `savings` (an estimate), as `this_month`. All zero or `null` for a month with no charge. |
| `month_days` | One row for every day of the month, oldest first (a day with no charge is a zero row, so a chart has every day), each with `period` (`YYYY-MM-DD`), `energy_kwh`, `cost`, `average_price_minor_per_kwh`, `solar_share`. |
| `month_sessions` | The month's closed sessions, newest first, in the shape of `sessions` (not cut by `limit`). |
| `available_months` | The months that have data, newest first, within the 24 months `month` accepts. |

A session counts on the local day, and month, it started. Additive fields: `cost_basis` on the answer
(`"current_settings"`: every cost is calculated at read time from the stored energy and raw spot prices
with the person's taxes and fees as they are now, so correcting a setting corrects the history) and on
each session (`"stored"` for an old record that kept only the cost it was written with), and on each
session `source`: `null` for a charge recorded live, `"imported_hourly"` for one rebuilt from Home
Assistant's hourly statistics (its `started_by` is then `"unknown"` and its `solar_share` `null`). A client
ignores values it does not know. With `format: "csv"` and a `month` the CSV is
that whole month and the file is named by its first and last day; `month` together with `from` or `to` is
refused.

### Power by month

`spotnav/get_power_month {"api_version": 1, "charger_id" | "site_id", "month"}` (any signed-in user, as the
dashboard) answers one month of the site's measured net import, the history dialog's *Power* view. `month` is
`"YYYY-MM"` on the area's clock, optional (the running month), at most 24 months back and never ahead. The webhook's
`get_power_month` answers the same for the charger's site (`month` and an optional `api_version`), with `"ok": true`
and `"action": "get_power_month"` beside it. The dashboard's `charger.capabilities` carries `get_power_month`, `true`
where the webhook answers the action.

| Field | Meaning |
| --- | --- |
| `api_version`, `site_id`, `month` | `1`, the site, the month shown. |
| `available_months` | The months with measured hours, newest first, within the 24 months `month` accepts. |
| `unit` | `"kW"`: every power below is an hourly mean of net import. |
| `import_target_kw` | The site's import target now, `null` for none. |
| `components` | The demand-charge parts the month was measured against, as `grid_tariff.demand.components`: those set now for the running month and the month before, those set when its last hour completed for an older month. |
| `days` | One row per day with a measured hour, oldest first: `date`, `peak_kw` (the day's highest hourly mean, among its complete hours where it has one), `peak_start` (that hour's start, local time with its offset), `house_kw` (the house's part of that hour, without what the chargers drew; `null` when they could not be read), `counted` (the indexes into `components` whose counted peaks fall on this day; a night part's peak after midnight counts on the date it falls on), `held` and `paused` (the import target held a charger's current down, or paused a charger, in any hour of the day), `imported` (the peak hour was read from Home Assistant's statistics, not measured) and `month_peak_kw` (the level the month's peak lifted the import target to when the peak hour ran, `null` where it did not). |
| `hours` | For the running month and the month before: every measured hour, oldest first, `{start, kw, house_kw, incomplete, held, paused, counted, imported, month_peak_kw}` (`incomplete`: more than ten minutes without a reading, never a peak; `imported` and `month_peak_kw` as on a day). `null` for an older month, which keeps its days only. The hour running now is not among them. |
| `bars` | Per part, its figure that month (`null` while nothing counted), the same as `grid_tariff.demand.per_component[].bar_kw` for the running month. |

Refusals: `spotnav_unsupported_api_version`, `spotnav_charger_required` (neither id), the charger lookup's codes,
`spotnav_unknown_site`, `spotnav_no_site` (the charger is in no site, or the site is not loaded),
`spotnav_needs_total_grid_power` (the site cannot measure its import: a direct site without its total grid power)
and `spotnav_invalid_range` (the month). The site keeps each hour of the running month and the month before, and a
summary of each day for 24 months. The months before SpotNav measured are filled once from Home Assistant's
statistics, three months back (`imported: true`; they count in the parts' peaks, and in `month_peak_kw` only when
complete, see
[earlier months](site-and-load-balancing.md#earlier-months-from-home-assistants-statistics)); while no earlier month
is kept, a request asks for that once more at most once a day. `imported` and `month_peak_kw` are additive: a reader
treats them as `false` and `null` where absent. Example answers are in `tests/fixtures/power_month/`
(`pilot_imported_month.json` has imported days and the month's peak) and
`tests/fixtures/webhook/get_power_month_*.json`.

### A suggested import target

`spotnav/suggest_import_target {"api_version": 1, "charger_id" | "site_id"}` (administrators only, as the target's
write; `spotnav_not_admin` otherwise) reads Home Assistant's hourly statistics of the site's grid power and its
chargers' draw for the last three months and answers a suggested import target. The webhook's
`suggest_import_target` answers the same for the charger's site (an optional `api_version`), with `"ok": true` and
`"action": "suggest_import_target"` beside it; the dashboard's `charger.capabilities` carries
`suggest_import_target`. The answer is kept per site for ten minutes.

| Field | Meaning |
| --- | --- |
| `api_version`, `site_id` | `1`, the site. |
| `available`, `reason` | Whether there is a suggestion; else why: `no_recorder`, `no_grid_statistics` (the grid power has no long-term statistics, or in a unit not known), `too_few_days` (fewer than seven), `no_chargers`. Every figure below is then `null`. |
| `suggestion_kw` | The house's typical daily peak plus the car's minimum power, rounded up to the next 0.5 kW (see [the rule](site-and-load-balancing.md#a-suggested-import-target)). |
| `typical_kw` | The median of the days' highest hourly means of the house alone. |
| `highest` | `{kw, start, date}`: the house's highest hour (local time). |
| `car_min_kw`, `car_kw` | The car's minimum power on the site, and what it gets at the suggestion in a typical evening or night hour (never below its minimum). |
| `counted` | `windows` where the daily peaks are taken within the demand-charge parts' own hours, else `all`. |
| `days`, `hours` | How many days and hours were read. |
| `period` | `{start, end}` read (local time). |
| `months` | Per month, oldest first: `{month, peak_kw, peak_start, typical_kw, days}`. |
| `grid_entities` | The grid power's entities read. |
| `chargers` | Per charger `{charger_id, source, has_statistics}`; `source` is `energy`, `power`, `current` or `null`. A charger without statistics counts as nothing: the grid alone stands for the house. |

Refusals as `spotnav/get_power_month`'s (without `spotnav_invalid_range`), and `spotnav_not_admin`. Examples:
`tests/fixtures/target_suggestion/pilot.json`, `tests/fixtures/webhook/suggest_import_target_success.json`.
