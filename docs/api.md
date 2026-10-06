# Apps and API

Everything the card and the SpotNav app do goes through two transports that share one set of
contracts: authenticated **WebSocket commands** (for the card and other Home Assistant clients)
and a per-charger **webhook** (for clients that cannot open a WebSocket, such as the app). All
contracts are version 1; each response carries `api_version`. A request for another version is
refused with `spotnav_unsupported_api_version`.

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
An instance-wide `pairing_uri` is also available on the *App pairing (all chargers)* sensor for manual setup.
Treat webhook ids as secrets and do not share the sensor's attributes.

## Charger webhook

`POST /api/webhook/<webhook id>` with a JSON body `{"version": 1, "action": ..., ...}`. The
webhook is bound to its own charger. A `charger_id` in the body is ignored. Success is
`{"ok": true, "action": ...}`; errors are `{"ok": false, "error": ...}` with HTTP 400 (bad
request), 409 (refused by the automatic-execution boundary, with a stable code), 429 (per-vehicle rate
limit, with `Retry-After`) or 502 (the charger command failed). A `start` or an immediate `stop` the
charger executed whose pause could not be saved answers `{"ok": true, "action": ..., "warning":
"reconcile_failed"}`: the pause holds in memory and the save is retried.

| Action | Effect |
| ------ | ------ |
| `dashboard` | The dashboard document below, for this charger. Takes an optional `api_version`. |
| `sessions` | The charge history, the same request and answer as `spotnav/get_sessions` below (`month`, `format`, `limit`, `from`, `to`), with `"ok": true` and the `action` beside it. A refusal is HTTP 400 with `{"ok": false, "error": "spotnav_invalid_range" \| "spotnav_unsupported_api_version", "action": "sessions"}`. Nothing is withheld. |
| `settings` | Replace the charger's settings: `{"expected_revision": n, "settings": {...}}`. |
| `start` | Start charging now; optional `amps`. Pauses automatic execution for the plug-in session (pause choice `manual`). |
| `stop` | Stop now, which pauses automatic execution for the plug-in session (pause choice `manual`), or with a `choice` (`next_period`, `until_tomorrow`, `until_resumed`) pause it for that span instead. |
| `resume` | Clear a pause. |
| `refresh_vehicle` | Re-read the vehicle's Home Assistant entities (`vehicle_id`); never wakes the car. |
| `set_charge_limit` | Write the vehicle's charge-limit entity (`vehicle_id`, `percent`). |
| `update_vehicle` | Change a vehicle's capacity, consumption or onboard charger, with `expected` values. |
| `update_site_settings` | Change solar priority or forecast sources of the charger's site. |
| `update_charger_priority` | Change this charger's priority on its site: `{"priority", "expected"}` (see below). |
| `push_register` | The app's instant notifications: `{"push_ref", "events"}`, or `{"push_ref": null}` to stop (see below). |

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
`plan_at_risk`, `charge_complete`, `charge_started`, `plugged_in`, `unplugged`, `plan_installed`;
default the first three), `url` (a Home Assistant path a tap opens, or `null` for the default
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

**Instant notifications.** `push_register` takes `push_ref`, the opaque reference the SpotNav relay
gave the app for its Firebase token (base64url text, at most 512 characters), or `null` to stop, and an
optional `events` (the notification event ids above; default `plan_stopped`, `plan_at_risk`,
`charge_complete`). It is stored per charger and kept across restarts; the answer is `{"ok": true,
"action": "push_register"}`, a bad body HTTP 400 `{"ok": false, "error": "invalid_push_register",
"action": "push_register"}`. When one of those events happens, Home Assistant posts `{"v": 1,
"push_ref"}` to the relay's `/v1/push/wake` (10 s, never retried, the same event not again within 15
minutes, at most twelve an hour), and the relay sends the app an empty wake-up; the app then reads
this charger as usual. Nothing about the charge goes to the relay. A relay answer of 404
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

## WebSocket commands

Every command takes `api_version: 1` and (except `list_chargers`) a `charger_id`, the charger's
config entry id. Reading is open to every authenticated user; writes require an administrator
(`spotnav_not_admin` otherwise).

| Command | Purpose |
| ------- | ------- |
| `spotnav/list_chargers` | The chargers on this instance (`id`, `name`). |
| `spotnav/get_dashboard` | The dashboard document for one charger. |
| `spotnav/get_settings`, `spotnav/update_settings` | Read and replace the settings record. |
| `spotnav/manual_action` | `start`, `stop` (with an optional pause `choice`) or `resume`. |
| `spotnav/get_market_options` | Price areas from the relay and their suggested fiscal values. |
| `spotnav/find_region` | A Great Britain postcode to its price region (`GB-A` … `GB-P`): `{"postcode": "SW1A 1AA"}` answers `region` (one), `regions` (the relay-listed ones) and `reason` (`null`, `invalid_postcode`, `not_found`, `unavailable`). Home Assistant asks Octopus Energy's public lookup directly; the postcode never reaches the relay and is neither stored nor logged. |
| `spotnav/get_entity_config`, `spotnav/update_entity_config` | The entities a charger and its site use. |
| `spotnav/choose_vehicle_soc` | Choose (or clear) a vehicle's state-of-charge sensor. |
| `spotnav/update_vehicle` | A vehicle's battery capacity, consumption and onboard charger. |
| `spotnav/update_site_settings` | Solar priority, forecast sources, active load balancing. |
| `spotnav/get_debug_bundle` | The redacted installation-wide debug bundle (administrators only). |
| `spotnav/get_card_info` | Which card the integration serves, for any signed-in user: `{"api_version": 1, "ok": true, "error": null, "spotnav_version", "card_bundle_hash"}`. The card compares the hash with the one in the URL it was loaded from. Not a dashboard field, so an older card is never handed a key it does not know. |
| `spotnav/get_sessions` | A charger's charge sessions: summaries per month and day and the latest sessions, the same for one chosen `month`, or with `format: "csv"` and optional `from` and `to` dates (or a `month`) the sessions of that range as CSV text. |

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

## Home Assistant events

Each charger has one **Charger events** entity (an `event` entity). It fires:

| `event_type` | When | Attributes |
|---|---|---|
| `charge_started` | the charger starts charging | `energy_register_kwh` |
| `charge_finished` | the charger stops charging | `unplugged`, and `energy_kwh` the charger's energy register counted since the start, when it has one |
| `plugged_in` / `unplugged` | the vehicle is connected / disconnected, for a charger that can say | none |
| `plan_installed` | a charging plan different from the one before is installed, by Auto or by hand | `start`, `end`, `periods`, `amps`, `energy_kwh`, `automatic` |
| `plan_at_risk` | the departure cannot be met with the energy that remains (the plan is the best effort, or not one whole quarter-hour is left); once each time it becomes so | `departure_time`, `requested_kwh` |

Starting Home Assistant is not an event. Example, a notification when a charge ends:

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
            Charging finished
            {{ trigger.to_state.attributes.energy_kwh | default('?') }} kWh.
```

## The dashboard document

`dashboard` returns one JSON document, enough for a client to render a charger without further
reads. Its main blocks: `charger` (identity, availability and `capabilities`), `chargers`,
`settings` (the canonical record with `revision`), `fiscal`, `market`, `prices` (the price
intervals), `plan` and `planning` (proposal, installed plan, and why), `control` (the immediate
action and the automatic action, with pause choices), `live`, `status` (typed status lines), `strategy`
and `strategy_state`, `vehicles` and `soc`, `site`, `charging_phases`, `phase_detection`, `charge_progress`, `progress`.
Example documents are in `tests/fixtures/dashboard/`.

**A person's own pause.** A Start or Stop pauses automatic execution for the plug-in session: the
`control.pause` record then carries the choice `manual` with two more keys, `action` (`start` or `stop`)
and `scope` (`plug_in`, the plug-in the car is in, or `next_plug_in` for a Stop given with no car), and no
`expires_at`. `manual` is never one of the `pause_choices` and cannot be asked for; the automatic action
beside it is `resume` (a `stop` with another choice replaces it). The `paused` status line carries
`action` and `ends` (`unplug`, `next_plug_in`, or `resume` on a charger that cannot tell when a car is
plugged in); both are null for every other pause. Every other pause record keeps its three keys.

**Battery room.** The `soc` block's additive `room_kwh` is the wall energy the battery still has room for,
to the car's own charge limit (else 100 %): `capacity_kwh x (ceiling - value) / 100 / efficiency`, `null`
without a level or a battery size. A manual amount is planned at most that much; when it is capped the
status carries `need_limited_by_room` (`kwh`), and while a Start is in effect for a plan the car ends
itself (a capped amount, or a target at or above the car's own limit) it carries
`charging_to_vehicle_limit` (`percent`, the car's limit, 100 when it states none). A client shows its kWh
slider up to `room_kwh` when it is present; an older backend has neither field nor line. Under "Fill"
(`fill_to_limit`) the status carries `filling_to_limit` (`kwh`, the room now) in place of
`need_limited_by_room`, and, while no room is known, the notice `fill_room_unknown` (`kwh`, the stored amount
planned instead). The card's slider goes past the room to `min(capacity to the car's limit, max(30 kWh,
2 x room))`, rounded up to its half-kWh step, with a "full" mark at the room; its last step is "Fill".

**Waiting for the car's new level.** With a target, the planning state `nothing_to_charge` has the reason
`waiting_for_vehicle_update` when the charge that just ended delivered, by measurement, at least what the
car's last reading needed and the car has not reported since; the status headline is then
`waiting_for_vehicle_update` (no params) in place of `nothing_to_charge`. A client that does not know the
code shows it as any unknown code.

**Best effort before a departure.** When the need cannot be met by the departure, the plan is every
whole quarter-hour from the first usable one up to the departure (one run, whatever `max_periods` says,
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

**Price areas from relay contract v2.** `market` carries three additive fields: `market_timezone` (the
zone whose calendar day one relay day file covers; equal to `timezone` except for Great Britain,
`Europe/Paris` beside `Europe/London`, and Portugal, `Europe/Madrid` beside `Europe/Lisbon`), `included`
(the relay's names, `vat`, `tax`, `grid_fee`, of what the published price already contains) and `source`
(`{"name", "url"}`, where the prices come from, or `null`). Each `fiscal` component of an included part has
the policy `included`, with no effective value. `spotnav/get_market_options` gives every area the same
`market_timezone` and `source`, and `included` in the settings' own component names. Days in `prices` are
always days in `timezone`; a Great Britain day is cut from the two relay files that cover it.

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
and `solar_site_incomplete` (`phases`) while a direct site's phase measurement is unusable and solar runs
on the total grid power only. Every line naming an entity also carries the additive `entity_name` (its friendly name, the id when it has
none; `entity_names`, parallel to `entities`, on `site_meter_unavailable`), which clients prefer to the id.
`site_meter_unavailable` (`entities`, `cause`: `inverter_standby` or
`meter_unavailable`) replaces `site_measurement_problem` when the phases without a value are the meter's
sensors reading `unavailable`/`unknown` together: every phase at once, or a known inverter integration's
(SolaX, Huawei, SolarEdge, Fronius, ...), which go unavailable when the inverter is in standby at night.
The site sensor's `solar_surplus` rows carry the same facts as `basis_problem`, `basis_entity`,
`charger_current`, `charger_current_entity` and `site_incomplete_phases`.

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

A charger's `energy_register_entity` field in `get_entity_config` carries the additive `none`, as
`current_limit` does: `{"allowed": true, "chosen": bool, "automatic": {"entity_id", "friendly_name"} | null}`,
`automatic` being the lifetime register detection finds now, also while "None" is chosen.
`update_entity_config` takes `"none"` for it (no register, and none looked up or detected again) and `""`
for the one SpotNav finds itself; while "None" is chosen the field reads back as `"none"`.

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
