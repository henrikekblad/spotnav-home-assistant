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
limit, with `Retry-After`) or 502 (the charger command failed).

| Action | Effect |
| ------ | ------ |
| `dashboard` | The dashboard document below, for this charger. Takes an optional `api_version`. |
| `sessions` | The charge history, the same request and answer as `spotnav/get_sessions` below (`month`, `format`, `limit`, `from`, `to`), with `"ok": true` and the `action` beside it. A refusal is HTTP 400 with `{"ok": false, "error": "spotnav_invalid_range" \| "spotnav_unsupported_api_version", "action": "sessions"}`. Nothing is withheld. |
| `settings` | Replace the charger's settings: `{"expected_revision": n, "settings": {...}}`. |
| `start` | Start charging now; optional `amps`. |
| `stop` | Stop now, or with a `choice` (`next_period`, `until_tomorrow`, `until_resumed`) pause automatic execution. |
| `resume` | Clear a pause. |
| `refresh_vehicle` | Re-read the vehicle's Home Assistant entities (`vehicle_id`); never wakes the car. |
| `set_charge_limit` | Write the vehicle's charge-limit entity (`vehicle_id`, `percent`). |
| `update_vehicle` | Change a vehicle's capacity, consumption or onboard charger, with `expected` values. |
| `update_site_settings` | Change solar priority or forecast sources of the charger's site. |
| `update_charger_priority` | Change this charger's priority on its site: `{"priority", "expected"}` (see below). |

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
| `plan_at_risk` | the departure cannot be met with the energy that remains; once each time it becomes so | `departure_time`, `requested_kwh` |

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
and `strategy_state`, `vehicles` and `soc`, `site`, `charging_phases`, `phase_detection`, `charge_progress`.
Example documents are in `tests/fixtures/dashboard/`.

**Battery room.** The `soc` block's additive `room_kwh` is the wall energy the battery still has room for,
to the car's own charge limit (else 100 %): `capacity_kwh x (ceiling - value) / 100 / efficiency`, `null`
without a level or a battery size. A manual amount is planned at most that much; when it is capped the
status carries `need_limited_by_room` (`kwh`), and while a Start is in effect for a plan the car ends
itself (a capped amount, or a target at or above the car's own limit) it carries
`charging_to_vehicle_limit` (`percent`, the car's limit, 100 when it states none). A client shows its kWh
slider up to `room_kwh` when it is present; an older backend has neither field nor line.

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
