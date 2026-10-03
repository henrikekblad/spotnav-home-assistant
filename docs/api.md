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
| `dashboard` | The one read: the dashboard document below, for this charger. Takes an optional `api_version`. |
| `settings` | Replace the charger's settings: `{"expected_revision": n, "settings": {...}}`. |
| `start` | Start charging now; optional `amps`. |
| `stop` | Stop now, or with a `choice` (`next_period`, `until_tomorrow`, `until_resumed`) pause automatic execution. |
| `resume` | Clear a pause. |
| `refresh_vehicle` | Re-read the vehicle's Home Assistant entities (`vehicle_id`); never wakes the car. |
| `set_charge_limit` | Write the vehicle's charge-limit entity (`vehicle_id`, `percent`). |
| `update_vehicle` | Change a vehicle's capacity, consumption or onboard charger, with `expected` values. |
| `update_site_settings` | Change solar priority or forecast sources of the charger's site. |

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

Turning **active load balancing** on or off is not available through the webhook, only through
the WebSocket by an administrator.

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
| `spotnav/get_entity_config`, `spotnav/update_entity_config` | The entities a charger and its site use. |
| `spotnav/choose_vehicle_soc` | Choose (or clear) a vehicle's state-of-charge sensor. |
| `spotnav/update_vehicle` | A vehicle's battery capacity, consumption and onboard charger. |
| `spotnav/update_site_settings` | Solar priority, forecast sources, active load balancing. |
| `spotnav/get_sessions` | A charger's charge sessions: summaries per month and day and the latest sessions, or with `format: "csv"` and optional `from` and `to` dates the sessions of that range as CSV text. |

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

The additive `sessions_summary` block holds this month's and last month's charge sessions
(`sessions`, `energy_kwh`, `cost`, `currency`, `average_price_minor_per_kwh`, `solar_share`,
`savings`). Cost is in the major unit, prices in the minor unit per kWh; `savings` compares with
the day's average price and is an estimate (`savings_estimate: true`). A client ignores keys it does
not know. Example answers of `spotnav/get_sessions` are in `tests/fixtures/sessions/`.
